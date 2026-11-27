"""Дерево стадий интенций у проекта с ведущим — история (LEAD-SYSTEM-DESIGN 2.1, Р2).

08.11: у local-library и deep-research с 13.10 висели открытые стадии (delivered, handed_over,
quality, …) и 7 препятствий `procedure_unknown` — продукт ведёт ведущий по книге O-*, а дерево
стадий продолжало ждать «способ». Закрыть одни препятствия нельзя: открытый лист без работы —
нарушение no_next_step, и контроллер заведёт `plan`. Поэтому узлы дерева закрываются целиком
(`dropped`, событие `intent.stages_to_history`), их препятствия — `resolved` со ссылкой на решение.
Новое намерение владельца для такого проекта не строит дерево, а становится поручением ведущему.
"""
from __future__ import annotations

from typing import Any

DECISION = "superseded:LEAD-SYSTEM-DESIGN-2.1"


def lead_projects(tx: Any) -> set[str]:
    """Активные проекты, у которых есть живой устав ведущего."""
    return {str(r["project"]) for r in tx.query(
        "SELECT t.project FROM tasks t JOIN projects p ON p.name=t.project "
        "WHERE t.tz='TZ-LEAD' AND t.state<>'rejected' AND p.active=1")}


def is_lead_project(store: Any, project: str) -> bool:
    with store.read() as tx:
        return project in lead_projects(tx)


def retire(store: Any) -> dict[str, dict[str, int]]:
    """Открытые узлы дерева стадий проектов с ведущим → история. Идемпотентно."""
    done: dict[str, dict[str, int]] = {}
    with store.tx() as tx:
        for project in sorted(lead_projects(tx)):
            nodes = [str(r["id"]) for r in tx.query(
                "SELECT id FROM outcomes WHERE project=? AND state='open' AND id LIKE 'intent-%' "
                "AND COALESCE(intent_id,'') NOT LIKE 'product:%'", (project,))]
            if not nodes:
                continue
            marks = ",".join("?" * len(nodes))
            blockers = tx.scalar(f"SELECT COUNT(*) FROM obstacles WHERE state='open' "
                                 f"AND outcome_id IN ({marks})", tuple(nodes))
            tx.change(f"UPDATE obstacles SET state='resolved',change_source=? WHERE state='open' "
                      f"AND outcome_id IN ({marks})", (DECISION, *nodes),
                      event={"kind": "intent.stage_obstacles_superseded", "project": project})
            tx.change(f"UPDATE outcomes SET state='dropped',rev=rev+1 WHERE state='open' "
                      f"AND id IN ({marks})", tuple(nodes),
                      event={"kind": "intent.stages_to_history", "project": project,
                             "payload": {"nodes": nodes, "obstacles": int(blockers or 0),
                                         "decision": DECISION}})
            done[project] = {"nodes": len(nodes), "obstacles": int(blockers or 0)}
    return done


def intent_to_instruction(store: Any, project: str, *, raw_intent: str, source: str) -> str:
    """Намерение владельца для проекта с ведущим → поручение ведущему (вместо дерева стадий)."""
    import hashlib
    from .instructions import register
    text = " ".join(str(raw_intent or "").split())[:4000] or "(пустое намерение)"
    # Один id на ревизию источника: повтор необработанного события не плодит поручений.
    iid = "i-intent-" + hashlib.sha256(source.encode("utf-8")).hexdigest()[:10]
    return register(store, project, text="Намерение владельца: " + text,
                    done_when="ведущий принял его в книгу (O-*), отклонил с причиной или показал результат",
                    author="owner", source=source[:300], iid=iid, strict=False)
