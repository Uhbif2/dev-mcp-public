"""Human-first projection of the owner dashboard, using real temporary state."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from dev_mcp.fleet import outcome
from dev_mcp.fleet.store import Store
from dev_mcp.owner.overview import snapshot


@pytest.fixture()
def store(tmp_path):
    result = Store.open(str(tmp_path / "state.sqlite"))
    yield result
    result.close()


def project(store, tmp_path):
    root = tmp_path / "Дело-владельца"
    (root / "intent").mkdir(parents=True)
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root) VALUES (?,?)",
                  ("Дело-владельца", str(root)), event="test.seed")
    return root


def write_understood(root):
    (root / "intent" / "understood.md").write_text(
        "## Интенция\n"
        "Сделать удобную панель владельца\n"
        "\n"
        "## Нужно от владельца\n"
        "- Проверить результат; по умолчанию: продолжить автоматически; "
        "почему: требуется визуальное решение\n",
        encoding="utf-8",
    )


def seed_stages(store, *, last=0):
    names = ("understood", "executable_goal", "approach_selected", "executable_plan",
             "implemented", "clean_start", "quality", "delivered", "handed_over")
    for idx, name in enumerate(names):
        result = outcome.create_outcome(
            store, project="Дело-владельца", title=name, intent_id="intent-opaque",
            spec={"stage": name, "private_text": "PRIVATE-SPEC"},
            evidence_spec={"subject_version": "v1"},
            authority={"test": True},
        )
        if idx < last:
            outcome.add_evidence(
                store, writer=outcome.server_evidence_writer(store),
                outcome_id=result.id, kind="review", ref="SECRET-RAW-REF",
                subject_version="v1", env_version="sandbox", verdict="pass",
                producer="test",
            )
            outcome.close_outcome(store, result.id)


def test_human_readable_intent_owner_action_and_fleet(store, tmp_path):
    root = project(store, tmp_path)
    write_understood(root)
    seed_stages(store, last=1)
    with store.tx() as tx:
        tx.change(
            "INSERT INTO tasks(project,tz,state,created_at,updated_at) "
            "VALUES (?,?,'ready',?,?)",
            ("Дело-владельца", "TZ-1", "2026-10-01T00:00:00+00:00",
             "2026-10-01T00:00:00+00:00"), event="test.seed")
    payload = snapshot(store)
    assert payload["status"] == "ready", payload
    human = payload["human"]
    assert len(human["projects"]) == 1
    card = human["projects"][0]
    assert card["title"] == "Сделать удобную панель владельца"
    assert card["current"] == "Цель"
    assert card["description"] == "Проверяются критерии результата"
    assert [x["label"] for x in card["stages"]] == [
        "Понято", "Цель", "Способ", "План", "Сделано", "С нуля",
        "Качество", "Поставлено", "Передано",
    ]
    assert card["stages"][0]["done"] is True
    assert card["stages"][1]["done"] is False
    assert card["folder"] == root.as_uri()
    assert card["document"].endswith("/intent/goal.md")
    assert "T" in card["started"]
    assert human["fleet"]["queue"] == 1
    assert human["fleet"]["agents"] == 0
    assert human["fleet"]["overloads"] == 0
    action, = human["actions"]
    assert action["action"] == "Проверить результат"
    assert action["default"] == "продолжить автоматически"
    assert action["reason"] == "требуется визуальное решение"
    assert action["action_id"] == hashlib.sha256(
        ("Дело-владельца\nПроверить результат").encode()
    ).hexdigest()[:20]
    human_text = json.dumps(human, ensure_ascii=False)
    assert "intent-opaque" not in human_text
    assert "SECRET-RAW-REF" not in human_text
    assert "PRIVATE-SPEC" not in human_text


def test_owner_done_receipt_filters_action_without_mutating_on_read(store, tmp_path):
    root = project(store, tmp_path)
    write_understood(root)
    seed_stages(store)
    initial = snapshot(store)
    action, = initial["human"]["actions"]
    with store.tx() as tx:
        tx.record_event(kind="owner.done", project="Дело-владельца",
                        payload={"action_id": action["action_id"]})
    after = snapshot(store)
    assert after["human"]["actions"] == []
    assert after["revision"] != initial["revision"]
    with store.read() as tx:
        events_before = tx.scalar("SELECT COUNT(*) FROM events")
    assert snapshot(store)["human"]["actions"] == []
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events") == events_before


def test_versioned_intent_future_constraints_are_not_current_owner_requests(store, tmp_path):
    root = project(store,tmp_path)
    write_understood(root)
    seed_stages(store)
    with store.tx() as tx:
        tx.change("UPDATE outcomes SET spec=json_set(spec,'$.source_id','source')",event="seed.versioned")
    assert snapshot(store)["human"]["actions"] == []
    with store.read() as tx:
        row = tx.one("SELECT id FROM outcomes WHERE title='executable_goal'")
    blocked = outcome.open_obstacle(store,outcome_id=row["id"],type="owner_action",
        cause_code="owner_taste",predicate={},owner_action="Подтвердить конкретное решение сейчас")
    actions = snapshot(store)["human"]["actions"]
    assert [a["action"] for a in actions] == ["Подтвердить конкретное решение сейчас"]
    outcome.resolve_obstacle(store,blocked)
    assert snapshot(store)["human"]["actions"] == []


def test_ops_future_permission_is_not_current_approval_and_never_granted(tmp_path):
    from dev_mcp.owner.overview import current_ops_approval_requested
    (tmp_path/'intent').mkdir()
    path = tmp_path/'intent/rollback-plan.md'
    path.write_text('## Нужно от владельца\n**Для текущего документа — ничего.** Позже отдельное разрешение на пилот.\n',encoding='utf-8')
    assert current_ops_approval_requested(tmp_path) is False
    path.write_text('## Нужно от владельца\nОдобрить запуск ограниченного пилота по этому плану.\n',encoding='utf-8')
    assert current_ops_approval_requested(tmp_path) is True


def test_handover_and_empty_owner_section(store, tmp_path):
    root = project(store, tmp_path)
    (root / "intent" / "understood.md").write_text(
        "## Интенция\nЗапустить продукт\n## Нужно от владельца\nничего\n",
        encoding="utf-8",
    )
    (root / "intent" / "handover.md").write_text(
        "## Готово\nПередано и проверено на чистом окружении\n",
        encoding="utf-8",
    )
    seed_stages(store, last=9)
    payload = snapshot(store)["human"]
    assert payload["actions"] == []
    assert payload["projects"] == []
    assert payload["done"] == [{
        "project": payload["done"][0]["project"],
        "title": "Запустить продукт",
        "text": "Передано и проверено на чистом окружении",
    }]


def test_empty_snapshot_human_safe_and_no_symlink_follow(store, tmp_path):
    empty = snapshot(store)
    assert empty["human"]["actions"] == []
    assert empty["human"]["projects"] == []
    assert empty["human"]["done"] == []
    root = project(store, tmp_path)
    target = tmp_path / "foreign.md"
    target.write_text("## Интенция\nUNTRUSTED-DOCUMENT", encoding="utf-8")
    try:
        (root / "intent" / "understood.md").symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("symlink privileges unavailable")
    seed_stages(store)
    human = snapshot(store)["human"]
    assert human["projects"][0]["title"] == "Дело владельца"
    assert "UNTRUSTED-DOCUMENT" not in json.dumps(human, ensure_ascii=False)


def test_human_actions_capped_at_30_across_projects(store, tmp_path):
    """The dashboard cap applies across all projects, not once per project."""
    for index in range(36):
        name = f"Intent-{index:02d}"
        root = tmp_path / name
        (root / "intent").mkdir(parents=True)
        (root / "intent" / "understood.md").write_text(
            "## Интенция\nПроверяем действие\n"
            "## Нужно от владельца\n- Подтвердить результат\n",
            encoding="utf-8",
        )
        with store.tx() as tx:
            tx.change("INSERT INTO projects(name,root) VALUES (?,?)",
                      (name, str(root)), event="test.seed")
        outcome.create_outcome(
            store, project=name, title="understood", intent_id=f"intent-{index}",
            spec={"stage": "understood"}, evidence_spec={"subject_version": "v1"},
            authority={"test": True},
        )
    actions = snapshot(store)["human"]["actions"]
    assert len(actions) == 30
    assert len({action["project"] for action in actions}) == 30


@pytest.mark.parametrize(("kind", "stages", "label", "document"), [
    ("investigate", ("understood", "hypotheses_plan", "experiments", "verified", "report", "handed_over"),
     "Гипотезы", "hypotheses.md"),
    ("ops", ("understood", "rollback_plan", "owner_approved", "executed", "verified", "handed_over"),
     "Откат", "rollback-plan.md"),
])
def test_kind_stages_ignore_superseded_build_proofs(store, tmp_path, kind, stages, label, document):
    root = project(store, tmp_path)
    seed_stages(store, last=9)
    with store.tx() as tx:
        tx.change("UPDATE outcomes SET state='dropped'", event="test.superseded")
    parent = outcome.create_outcome(
        store, project="Дело-владельца", title="root", intent_id="new-intent",
        spec={"kind": kind, "size": "large"}, evidence_spec={"subject_version": "v2"},
        authority={"test": True})
    for stage in stages:
        item = outcome.create_outcome(
            store, project="Дело-владельца", title=stage, intent_id="new-intent",
            parent_id=parent.id, spec={"stage": stage, "kind": kind, "size": "large"},
            evidence_spec={"kind": "review", "subject_version": "v2"}, authority={"test": True})
        outcome.add_evidence(store, writer=outcome.server_evidence_writer(store),
                             outcome_id=item.id, kind="review", ref="test-proof",
                             subject_version="v2", env_version="test", verdict="pass", producer="test")
        if stage == "understood":
            outcome.close_outcome(store, item.id)
    card, = snapshot(store)["human"]["projects"]
    assert card["current"] == label
    assert card["document"] == (root / "intent" / document).as_uri()
    assert len(card["stages"]) == len(stages)
    assert card["stages"][0]["done"]
    assert all(not stage["done"] for stage in card["stages"][1:])
    assert "Цель" not in [stage["label"] for stage in card["stages"]]


def test_pending_delivery_is_not_owner_approval_and_inactive_queue_is_excluded(store, tmp_path):
    project(store, tmp_path)
    seed_stages(store)
    payload = {"kind": "needs", "sha256": "a" * 64, "toast_accepted": False}
    with store.tx() as tx:
        tx.record_event(kind="owner_channel.sent", project="Дело-владельца", payload=payload)
        tx.change("INSERT INTO projects(name,root,active) VALUES ('old','old',0)", event="test.seed")
        tx.change("INSERT INTO tasks(project,tz,state,created_at,updated_at) "
                  "VALUES ('old','TZ-1','ready','now','now')", event="test.seed")
    human = snapshot(store)["human"]
    assert human["fleet"]["pending_notices"] == 1
    assert human["fleet"]["queue"] == 0
    with store.tx() as tx:
        tx.record_event(kind="owner_channel.toast_attempt", project="Дело-владельца",
                        payload={**payload, "toast_accepted": True})
    assert snapshot(store)["human"]["fleet"]["pending_notices"] == 0
    with store.read() as tx:
        assert not tx.one("SELECT id FROM events WHERE kind IN ('owner.done','intent.ops_approved')")
