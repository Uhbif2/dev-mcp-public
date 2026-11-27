"""Admission and handoff for a bounded user outcome on the existing lead charter."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WINDOWS = 2
CHARTER = "TZ-LEAD"
EPOCH = "lead_focus_epoch"
WAIT = "lead_focus_wait"
RESULT = "lead_focus_result"
RECOVERY = "lead_focus_recovery"
CHOICE = "lead_focus_choice"
PARK = "lead_focus_park"


def continuous(project: str) -> bool:
    from .goals import project_info

    return project_info(project).get("lead_continuous") is True


def settings(project: str) -> dict:
    from .goals import project_info

    focus = project_info(project).get("lead_focus")
    return focus if isinstance(focus, dict) and focus.get("action") and focus.get("evidence") else {}


def _version(focus: dict) -> str:
    return hashlib.sha256(json.dumps(focus, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _latest(tx: Any, project: str, kind: str, after: int = 0) -> Any:
    return tx.one("SELECT id,ts,payload FROM events WHERE project=? AND kind=? AND id>? "
                  "ORDER BY id DESC LIMIT 1", (project, kind, after))


def _epoch(tx: Any, project: str, focus: dict) -> Any:
    row = _latest(tx, project, EPOCH)
    return row if row and json.loads(row["payload"])["version"] == _version(focus) else None


def _basis(tx: Any, project: str, epoch: Any) -> Any:
    from .instructions import KIND

    # Field sensors generate suggestions continuously. Only an owner directive
    # can replace the current user outcome or reopen a completed focus.
    owner = tx.one("SELECT id,ts,payload FROM events WHERE project=? AND kind=? AND id>? "
                   "AND json_extract(payload,'$.author') IN ('owner','operator') "
                   "ORDER BY id DESC LIMIT 1", (project, KIND, epoch["id"]))
    return owner or epoch


def _owner_instruction(basis: Any, focus: dict) -> str:
    payload = json.loads(basis["payload"])
    if payload.get("source") == focus.get("instruction_source"):
        return str(payload.get("id") or "")
    return ""


def _cycle(tx: Any, project: str, basis: Any) -> tuple[Any, Any, Any]:
    result = _latest(tx, project, RESULT, basis["id"])
    anchor = result or basis
    choice = _latest(tx, project, CHOICE, anchor["id"])
    return result, choice, choice or anchor


def _open_obstacle(tx: Any, project: str, obstacle: str) -> bool:
    from .obstacles import CLOSED, OPEN

    row = tx.one("SELECT kind FROM events WHERE project=? AND kind IN (?,?) "
                 "AND json_extract(payload,'$.id')=? ORDER BY id DESC LIMIT 1",
                 (project, OPEN, CLOSED, obstacle))
    return bool(row and row["kind"] == OPEN)


def _verified_prior_delivery(tx: Any, project: str, focus: dict, event_id: int) -> bool:
    """A release handoff can reuse the exact independently accepted artifact."""
    delivery = focus.get("prior_delivery")
    if not isinstance(delivery, dict) or delivery.get("event") != event_id:
        return False
    proof = tx.one("SELECT tz,payload FROM events WHERE id=? AND project=? AND kind='accept'",
                   (event_id, project))
    if not proof or proof["tz"] != delivery.get("task"):
        return False
    receipt = json.loads(proof["payload"])
    artifact_sha = delivery.get("artifact_sha")
    if not receipt.get("review_task") or not artifact_sha or artifact_sha not in receipt.get("note", ""):
        return False
    root = tx.scalar("SELECT root FROM projects WHERE name=?", (project,))
    if not root:
        return False
    base = Path(root).resolve()
    try:
        for field in ("artifact", "handoff"):
            path = (base / delivery[field]).resolve(strict=True)
            if not path.is_relative_to(base):
                return False
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != delivery[field + "_sha"]:
                    return False
    except (OSError, KeyError, TypeError, ValueError):
        return False
    return True


def _dependency(tx: Any, project: str, basis: Any) -> tuple[Any, Any, Any]:
    wait = _latest(tx, project, WAIT, basis["id"])
    if not wait:
        return None, None, None
    target = json.loads(wait["payload"])["tz"]
    task = tx.one("SELECT tz,state FROM tasks WHERE project=? AND tz=?", (project, target))
    if not task or task["state"] not in {"accepted", "rejected"}:
        return wait, task, None
    # The review task itself can be marked accepted by escalation. Wait on
    # the work it reviews, whose accept/reject is the actual decision.
    verdict = tx.one("SELECT id,ts FROM events WHERE project=? AND tz=? AND id>? "
                     "AND kind=? ORDER BY id DESC LIMIT 1",
                     (project, target, wait["id"], "accept" if task["state"] == "accepted" else "reject"))
    if verdict is None and task["state"] == "accepted":
        verdict = _repair_acceptance(tx, project, target, wait["id"])
    return wait, task, verdict


def _repair_acceptance(tx: Any, project: str, target: str, wait_id: int) -> Any:
    # Review accepts a repair and its source atomically, but writes one receipt on the repair.
    # Follow the recorded handoffs, including repairs opened before the lead started waiting.
    after = 0
    while handoff := tx.one(
        "SELECT e.id,t.tz FROM events e JOIN tasks t ON t.project=e.project "
        "AND t.tz=json_extract(e.payload,'$.fix_task') AND t.kind='fix' "
        "WHERE e.project=? AND e.tz=? AND e.kind='reject' AND e.id>? "
        "ORDER BY e.id DESC LIMIT 1", (project, target, after),
    ):
        target, after = handoff["tz"], handoff["id"]
        verdict = tx.one(
            "SELECT id,ts FROM events WHERE project=? AND tz=? AND id>? AND kind='accept' "
            "AND json_extract(payload,'$.review_task')<>'' ORDER BY id DESC LIMIT 1",
            (project, target, max(wait_id, after)),
        )
        if verdict:
            return verdict
    return None


def _windows(tx: Any, project: str, since: str) -> int:
    """Count closed turns with actual tool activity, excluding launch reservations."""
    return int(tx.scalar(
        "SELECT COUNT(*) FROM (SELECT c.id FROM claims c WHERE c.project=? AND c.tz=? "
        "AND c.state IN ('released','requeued') AND c.released_at>=? AND EXISTS ("
        "SELECT 1 FROM events e WHERE e.worker_id=c.worker_id AND e.ts>=MAX(c.started_at,?) "
        "AND e.ts<=c.released_at AND e.project=c.project AND e.tz=c.tz AND "
        "e.kind IN ('run','write','checkpoint','read')) LIMIT ?)",
        (project, CHARTER, since, since, WINDOWS),
    ) or 0)


def _open_handoff(tx: Any, project: str, after: int, dependency: str) -> Any:
    return tx.one("SELECT t.tz,t.state FROM tasks t WHERE t.project=? "
                  "AND t.state IN ('ready','taken','submitted','frozen') AND "
                  "(t.tz=? OR EXISTS (SELECT 1 FROM events e WHERE e.project=t.project "
                  "AND e.tz=t.tz AND e.kind='lead_issue' AND e.id>?)) "
                  "ORDER BY CASE WHEN t.tz=? THEN 1 ELSE 0 END, t.updated_at DESC LIMIT 1",
                  (project, dependency, after, dependency))


def _success(tx: Any, project: str, after: int, witness: str) -> Any:
    if not witness:
        return None
    return tx.one("SELECT id,ts FROM events WHERE project=? AND kind='witness_result' AND id>? "
                  "AND json_extract(payload,'$.id')=? AND json_extract(payload,'$.verdict')='works_live' "
                  "ORDER BY id LIMIT 1", (project, after, witness))


def issue_refusal(tx: Any, project: str) -> str:
    focus = settings(project)
    epoch = _epoch(tx, project, focus) if focus else None
    if epoch is None:
        return ""
    basis = _basis(tx, project, epoch)
    if continuous(project):
        owner = _owner_instruction(basis, focus)
        result, choice, basis = _cycle(tx, project, basis)
        if not choice and (result or not owner):
            return "Сначала зафиксируй выбранный результат через fleet_lead choose."
    # The single-target wait controls resumption of the charter, not admission
    # of independent delivery tracks. lead_admin._issue enforces overlapping
    # owns and exclusive resources against all unfinished lead subtasks.
    return ""


def state(tx: Any, project: str) -> tuple[str, str]:
    focus = settings(project)
    epoch = _epoch(tx, project, focus) if focus else None
    if epoch is None:
        return "", ""
    basis = _basis(tx, project, epoch)
    if continuous(project):
        result, choice, anchor = _cycle(tx, project, basis)
        park = _latest(tx, project, PARK, anchor["id"])
        if park:
            data = json.loads(park["payload"])
            if _open_obstacle(tx, project, data["obstacle"]):
                return "waiting", f"ждёт {data['obstacle']}: {data['summary']}"
        wait, task, verdict = _dependency(tx, project, anchor)
        if wait and verdict is None:
            data = json.loads(wait["payload"])
            actual = str(task["state"]) if task else "запись недоступна"
            return "waiting", f"ждёт {data['tz']} ({actual}): {data['summary']}"
        if choice:
            data = json.loads(choice["payload"])
            tail = f" Вердикт зависимости #{verdict['id']}." if verdict else ""
            return "selected", f"{data['obligation']}: {data['summary']} Проверка: {data['criterion']}.{tail}"
        if result:
            return "choose", f"поставка закрыта событием #{result['id']}. Выбери следующий результат проекта."
        return "", ""
    result = _latest(tx, project, RESULT, basis["id"])
    if result:
        proof = json.loads(result["payload"])
        return "done", f"результат передан, свидетельство #{proof['evidence']}: {proof['summary']}"
    wait, task, verdict = _dependency(tx, project, basis)
    if wait and verdict is None:
        data = json.loads(wait["payload"])
        actual = str(task["state"]) if task else "запись недоступна"
        return "waiting", f"ждёт {data['tz']} ({actual}): {data['summary']}"
    anchor = verdict or basis
    success = _success(tx, project, anchor["id"], str(focus.get("witness") or ""))
    anchor = success or anchor
    count = _windows(tx, project, anchor["ts"])
    if count >= WINDOWS:
        # Pick an already open handoff, including one inherited at deployment.
        # A blocked review stays on the dependency path rather than waking
        # another lead just to describe its status.
        opened = _open_handoff(tx, project, anchor["id"], str(focus.get("dependency") or ""))
        if opened:
            return "handoff", f"два захода без передачи результата, довести {opened['tz']} ({opened['state']})"
        recovery = _latest(tx, project, RECOVERY, anchor["id"])
        if recovery is None:
            return "needs_recovery", "два захода без результата, готовится один заход на устранение причины"
        if _windows(tx, project, recovery["ts"]) == 0:
            return "recovery", ("один заход на устранение причины: проверь первый сбой, исправь его "
                                "или передай одну точную зависимость")
        return "stalled", "заход на устранение причины исчерпан, результата и точной зависимости пока нет"
    if count == WINDOWS - 1:
        return "last", "это последний заход: доведи результат и передай его или укажи точную зависимость"
    return "", ""


def hold_reason(tx: Any, project: str, tz: str) -> str:
    if tz != CHARTER:
        return ""
    status, reason = state(tx, project)
    return reason if status in {"done", "waiting", "handoff", "needs_recovery", "stalled"} else ""


def _wait(tx: Any, project: str, worker: str | None, target: str, summary: str,
          *, result_task: bool = False) -> None:
    tx.record_event(kind=WAIT, project=project, tz=CHARTER, worker_id=worker,
                    payload={"tz": target, "summary": summary, "result_task": result_task})


def tick(store: Any) -> None:
    """Persist automatic handoff once. The normal queue keeps running its work/reviews."""
    with store.read() as tx:
        projects = [row["project"] for row in tx.query(
            "SELECT t.project FROM tasks t JOIN projects p ON p.name=t.project "
            "WHERE t.tz=? AND p.active=1", (CHARTER,))]
    for project in projects:
        focus = settings(project)
        if not focus:
            continue
        with store.tx() as tx:
            if not _epoch(tx, project, focus):
                tx.record_event(kind=EPOCH, project=project, tz=CHARTER,
                                payload={"version": _version(focus)})
                configured = str(focus.get("dependency") or "")
                target = tx.one("SELECT state FROM tasks WHERE project=? AND tz=?",
                                (project, configured)) if configured else None
                if target and target["state"] in {"ready", "taken", "submitted", "frozen"}:
                    _wait(tx, project, None, configured, "Завершить уже назначенную проверку этого результата.",
                          result_task=focus.get("dependency_delivers") is True)
            status, _ = state(tx, project)
            if status not in {"handoff", "needs_recovery"}:
                continue
            if tx.one("SELECT 1 FROM claims WHERE project=? AND tz=? AND state='active'", (project, CHARTER)):
                continue
            if status == "needs_recovery":
                tx.record_event(kind=RECOVERY, project=project, tz=CHARTER, payload={})
                continue
            epoch = _epoch(tx, project, focus)
            basis = _basis(tx, project, epoch)
            _, _, verdict = _dependency(tx, project, basis)
            anchor = verdict or basis
            success = _success(tx, project, anchor["id"], str(focus.get("witness") or ""))
            target = _open_handoff(tx, project, (success or anchor)["id"],
                                   str(focus.get("dependency") or ""))
            if target:
                _wait(tx, project, None, target["tz"], "Дождаться вердикта по уже начатой работе.")


def lines(store: Any, project: str) -> list[str]:
    focus = settings(project)
    if not focus:
        return []
    with store.read() as tx:
        status, reason = state(tx, project)
        epoch = _epoch(tx, project, focus)
        if epoch and status in {"", "last", "recovery"}:
            wait, _, verdict = _dependency(tx, project, _basis(tx, project, epoch))
            if verdict:
                target = json.loads(wait["payload"])["tz"]
                tail = f"свежий вердикт по {target} — событие #{verdict['id']}"
                reason = f"{reason}. {tail}" if reason else tail
    lines = [f"Фокус ведущего ({project}) — {reason}"] if reason else []
    if continuous(project):
        from .lead import delivery_lines
        from .delivery_gap import summary_lines

        lines.extend(delivery_lines(store, project))
        with store.read() as tx:
            lines.extend(summary_lines(tx, project))
    return lines


def brief_lines(store: Any, project: str) -> list[str]:
    focus = settings(project)
    if not focus:
        return []
    if focus.get("orchestrated"):
        prior = focus.get("prior_delivery") or {}
        status = ""
        pilot_open = True
        choosing = continuous(project)
        if choosing:
            with store.read() as tx:
                status, _ = state(tx, project)
                epoch = _epoch(tx, project, focus)
                basis = _basis(tx, project, epoch) if epoch else None
                pilot_open = bool(basis and _owner_instruction(basis, focus)
                                  and not _latest(tx, project, RESULT, basis["id"]))
        return [
            *lines(store, project),
            ("Перед выбором и новой выдачей сверь поручение владельца, последнее свидетельство и доставку. "
             "Если принятую версию уже можно передать, проверь, что она открывается в месте использования, "
             "и передай её с точным остатком. Если этому мешает конкретный затор, оцени, быстрее ли "
             "снять его, чем начать новую работу. При внешней зависимости зафиксируй условие, "
             "ответственного и доступный полезный шаг. Явное поручение владельца имеет приоритет. "
             "Для самостоятельного выбора или смены запиши fleet_lead choose "
             "{claim_id, obligation, summary, criterion}. В summary укажи, что станет доступно "
             "пользователю и какой факт делает этот шаг первым. Повтор проверки обоснуй новым входом "
             "и решением, которое изменится от её результата."
             if choosing else
             "Первый ход: сверь исходное поручение, последний факт и место получения результата. "
             "Назови первый разрыв, выбери одно действие и начни его в этом же ходе."),
            "Ты ведёшь результат до передачи владельцу: крупную реализацию отдай исполнителю, "
            "независимую проверку — другому. Перед выдачей укажи ожидаемый результат, "
            "способ проверки и кто доведёт до места использования. Сам снимай прямые заторы.",
            "Если работа уже назначена, продолжай по её точному ID. fleet_lead wait фиксирует "
            "ожидание и возвращает устав после вердикта. Фоновый дефект бери, если он блокирует результат.",
            "Тесты проверяют регрессии. Закрывай поручение через fleet_lead result с event_id, "
            "ссылкой, проверенным сценарием и остатком.",
            *(["Если полезное продолжение зависит от внешнего условия, зарегистрируй препятствие "
               "и используй fleet_lead park {claim_id, obstacle, summary}. Снятие условия вернёт устав."]
              if choosing else []),
            *([f"Для уже принятой поставки открой {prior['handoff']} и проверь файл; "
               f"при совпадении используй event_id={prior['event']} в fleet_lead result."]
              if prior and pilot_open and status not in {"choose", "selected"} else []),
        ]
    return [
        *lines(store, project),
        "Два рабочих захода на один пользовательский результат. Проверь указанный путь, "
        "исправь первый доказанный дефект и передай результат.",
        "Сдай правки. Для передачи результата используй fleet_lead result {claim_id, event_id, summary}. "
        "Укажи ссылку или путь к результату, проверенный сценарий и существенный остаток.",
        "Если продолжение зависит от одной задачи, используй fleet_lead wait {claim_id, tz, summary}. "
        "Флот продолжит после её вердикта. Добавь result_task=true, если сама задача даёт "
        "проверенный результат пользователю.",
    ]


def settle(store: Any, worker: str, project: str, action: str, params: dict) -> str:
    summary = str(params.get("summary") or "").strip()[:500]
    if len(summary) < 25:
        raise ValueError(f"{action}: укажи результат для пользователя и конкретный остаток")
    focus = settings(project)
    with store.tx() as tx:
        epoch = _epoch(tx, project, focus) if focus else None
        claim = tx.one("SELECT id FROM claims WHERE worker_id=? AND project=? AND tz=? AND state='active'",
                       (worker, project, CHARTER))
        if epoch is None or claim is None:
            raise ValueError("нужен активный TZ-LEAD проекта с настроенным фокусом")
        if type(params.get("claim_id")) is not int or params["claim_id"] != claim["id"]:
            raise ValueError("FLEET_STALE_CLAIM: передай claim_id активного TZ-LEAD")
        basis = _basis(tx, project, epoch)
        instruction_id = _owner_instruction(basis, focus) if focus.get("orchestrated") else ""
        autonomous = continuous(project)
        result, choice, anchor = _cycle(tx, project, basis) if autonomous else (None, None, basis)
        if result:
            instruction_id = ""
        if action == "choose":
            from .obstacles import ledger_obligations

            if not autonomous or instruction_id:
                raise ValueError("choose: сначала доведи активную поставку владельца")
            obligation = str(params.get("obligation") or "").strip()
            criterion = str(params.get("criterion") or "").strip()[:500]
            root = tx.scalar("SELECT root FROM projects WHERE name=?", (project,))
            obligations = ledger_obligations(Path(root)) if root else None
            if obligation not in {o["id"] for o in obligations or []} or len(criterion) < 15:
                raise ValueError("choose: нужны O-ID из книги и проверяемый criterion")
            payload = {"obligation": obligation, "summary": summary, "criterion": criterion}
            if choice and json.loads(choice["payload"]) == payload:
                return f"{project}: результат уже выбран, продолжай предметное действие."
            tx.record_event(kind=CHOICE, project=project, tz=CHARTER, worker_id=worker, payload=payload)
            return f"{project}: выбран {obligation}. Продолжай работу в этом захвате."
        if autonomous and not instruction_id and not choice and action != "park":
            raise ValueError("Сначала выбери следующий результат через fleet_lead choose.")
        if action == "park":
            obstacle = str(params.get("obstacle") or "").strip()
            if not autonomous or not _open_obstacle(tx, project, obstacle):
                raise ValueError("park: нужно открытое препятствие проекта с условием возобновления")
            tx.record_event(kind=PARK, project=project, tz=CHARTER, worker_id=worker,
                            payload={"obstacle": obstacle, "summary": summary})
        elif action == "wait":
            target = str(params.get("tz") or "").strip()
            task = tx.one("SELECT kind,state FROM tasks WHERE project=? AND tz=?", (project, target))
            if (not task or task["kind"] not in {"work", "fix", "request"} or "TZ-LEAD" in target.upper()
                    or task["state"] not in {"ready", "taken", "submitted", "frozen"}):
                raise ValueError("wait: нужна точная незавершённая задача своего проекта (работа, fix или request)")
            _wait(tx, project, worker, target, summary, result_task=params.get("result_task") is True)
            if instruction_id:
                from .instructions import TARGET_KIND
                from .model import task_root

                tx.record_event(kind=TARGET_KIND, project=project, tz=CHARTER, worker_id=worker,
                                payload={"id": instruction_id, "task": task_root(target)})
        else:
            evidence = params.get("event_id")
            if type(evidence) is not int:
                raise ValueError("result: нужен номер нового события свидетельства")
            pinned_delivery = bool(instruction_id and focus.get("prior_delivery"))
            prior_delivery = pinned_delivery and _verified_prior_delivery(tx, project, focus, evidence)
            if pinned_delivery and not prior_delivery:
                raise ValueError("result: нужна квитанция и совпадающие байты настроенной поставки")
            if evidence <= anchor["id"] and not prior_delivery:
                raise ValueError("result: нужен номер нового события свидетельства")
            proof = tx.one("SELECT kind,payload FROM events WHERE id=? AND project=?", (evidence, project))
            wait, _, verdict = _dependency(tx, project, anchor)
            witness = json.loads(proof["payload"]) if proof and proof["kind"] == "witness_result" else {}
            accepted = (proof and proof["kind"] == "accept" and verdict and verdict["id"] == evidence
                        and wait and json.loads(wait["payload"]).get("result_task") is True
                        and json.loads(proof["payload"]).get("review_task"))
            measured = (bool(focus.get("witness")) and witness.get("id") == focus["witness"]
                        and witness.get("verdict") == "works_live"
                        and tx.scalar("SELECT MAX(id) FROM events WHERE project=? AND kind='witness_result' "
                                      "AND json_extract(payload,'$.id')=?", (project, focus["witness"])) == evidence)
            if not measured and not accepted and not prior_delivery:
                raise ValueError("result: нужен действующий works_live фокуса или независимый accept "
                                 "задачи, переданной с result_task=true, либо настроенная точная поставка")
            tx.record_event(kind=RESULT, project=project, tz=CHARTER, worker_id=worker,
                            payload={"evidence": evidence, "summary": summary})
            if instruction_id:
                from .instructions import CLOSED, STATUS_KIND

                status = tx.one("SELECT payload FROM events WHERE project=? AND kind=? "
                                "AND json_extract(payload,'$.id')=? ORDER BY id DESC LIMIT 1",
                                (project, STATUS_KIND, instruction_id))
                if not status or json.loads(status["payload"]).get("status") not in CLOSED:
                    tx.record_event(kind=STATUS_KIND, project=project, tz=CHARTER, worker_id=worker,
                                    payload={"id": instruction_id, "status": "done", "by": worker,
                                             "event_id": evidence, "evidence": summary})
        now = datetime.now(timezone.utc).isoformat()
        tx.changes(
            [("UPDATE claims SET state='released',released_at=? WHERE id=?", (now, claim["id"])),
             ("UPDATE tasks SET state='ready',updated_at=?,author_worker=CASE WHEN ? THEN ? ELSE author_worker END "
              "WHERE project=? AND tz=? AND state='taken'",
              (now, autonomous and action == "result", worker, project, CHARTER))],
            event={"kind": "lead_focus_release", "project": project, "tz": CHARTER, "worker_id": worker,
                   "payload": {"claim_id": claim["id"], "action": action}},
        )
    if autonomous and action == "result":
        return f"{project}: результат передан. Возьми устав через fleet_take и выбери следующий результат."
    return f"{project}: фокус {'передан' if action == 'result' else 'ждёт зависимости'}, захват освобождён."
