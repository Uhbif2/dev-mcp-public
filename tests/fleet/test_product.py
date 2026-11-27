"""LEAD-SYSTEM-DESIGN 2.1: обязательства продукта в реестре, состояние — из доказательств."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dev_mcp.fleet import outcome, product
from dev_mcp.fleet.store import Store

HEAD = "a" * 40


@pytest.fixture()
def env(tmp_path: Path):
    root = tmp_path / "Proj"
    (root / "ToDo" / "lead").mkdir(parents=True)
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    outcome.install_schema(store)
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES('P',?,1,1)", (str(root),),
                  event={"kind": "test_setup"})
    product._seen.clear()
    return store, root


def ledger(root: Path, *items: dict) -> None:
    (root / "ToDo" / "lead" / "LEDGER.json").write_text(
        json.dumps({"obligations": list(items)}, ensure_ascii=False), encoding="utf-8")


def witness(store: Store, obligation: str, verdict: str, *, head: str = HEAD,
            sha: str = "s1", wid: str = "w1") -> None:
    payload = {"id": wid, "obligation": obligation, "verdict": verdict, "head": head,
               "instrument_sha": sha, "rc": 0}
    with store.tx() as tx:
        tx.record_event(kind="witness_result", project="P", tz=f"witness:{wid}", payload=payload)
        eid = int(tx.scalar("SELECT MAX(id) FROM events"))
        product.record_witness(tx, "P", eid, payload)


def test_projection_is_one_way_and_idempotent(env):
    store, root = env
    ledger(root, {"id": "O-a", "source": "intent/goal.md", "promise": "x", "status": "works_live"},
           {"id": "O-b", "source": ["S01"], "promise": "y", "required_level": "L2"},
           {"id": "not-an-obligation"})
    assert product.sync(store)["P"]["added"] == 2
    assert product.sync(store) == {}
    with store.read() as tx:
        rows = {r["id"]: r for r in tx.query("SELECT * FROM outcomes WHERE project='P'")}
    assert set(rows) == {"P/product", "P/O-a", "P/O-b"}
    assert rows["P/O-a"]["intent_id"] == "product:P" and rows["P/O-a"]["parent_id"] == "P/product"
    assert json.loads(rows["P/O-b"]["evidence_spec"])["required_level"] == "L2"
    assert "status" not in rows["P/O-a"]["spec"]  # статус книги платформа не читает
    # Статус «works_live» в книге не делает обязательство выполненным: доказательств нет.
    assert [i["level"] for i in product.state(store, "P", head=HEAD)] == ["L0", "L0"]
    ledger(root, {"id": "O-a", "source": "intent/goal.md", "promise": "x2"})
    stats = product.sync(store)["P"]
    assert stats["changed"] == 1 and stats["dropped"] == 1
    with store.read() as tx:
        assert tx.scalar("SELECT rev FROM outcomes WHERE id='P/O-a'") == 2
        assert tx.scalar("SELECT state FROM outcomes WHERE id='P/O-b'") == "dropped"


def test_controller_invariant_ignores_product_subtree(env):
    """Без исключения контроллер завёл бы «план» на каждое O-* (no_next_step)."""
    store, root = env
    ledger(root, *({"id": f"O-{i}", "promise": "p"} for i in range(5)))
    product.sync(store)
    assert [v for v in outcome.check_invariant(store) if v.outcome_id.startswith("P/")] == []


def test_state_comes_from_evidence(env):
    store, root = env
    ledger(root, {"id": "O-live", "promise": "p"}, {"id": "O-broken", "promise": "p"},
           {"id": "O-unknown", "promise": "p"}, {"id": "O-tool", "promise": "p"})
    product.sync(store)
    witness(store, "O-live", "works_live")
    witness(store, "O-broken", "works_live", wid="w2")
    witness(store, "O-broken", "broken", wid="w2")
    witness(store, "O-unknown", "unknown", wid="w3")
    witness(store, "O-tool", "unknown", wid="w4", sha="s1")
    witness(store, "O-tool", "works_live", wid="w4", sha="s2")  # прибор поправили — и «позеленело»
    items = {i["id"]: i for i in product.state(store, "P", head=HEAD)}
    # Уровень виден, но прибор не принят с отрицательным контролем — требуемое не достигнуто.
    assert items["O-live"]["level"] == "L3" and not items["O-live"]["met"]
    assert items["O-live"]["unaccepted_instrument"]
    with store.tx() as tx:
        tx.record_event(kind="instrument_accepted", project="P", tz="witness:w1",
                        payload={"cell": "w1", "sha": "s1", "accepted": True})
    items = {i["id"]: i for i in product.state(store, "P", head=HEAD)}
    assert items["O-live"]["met"] and not items["O-live"]["unaccepted_instrument"]
    assert items["O-broken"]["broken"] and items["O-broken"]["level"] == "L0"
    assert not items["O-unknown"]["measured"] and items["O-unknown"]["level"] == "L0"
    assert items["O-tool"]["instrument_changed"] and items["O-tool"]["level"] == "L0"
    # Новый HEAD — доказательство устарело, требуемый уровень не засчитан.
    moved = {i["id"]: i for i in product.state(store, "P", head="b" * 40)}
    assert moved["O-live"]["stale"] and not moved["O-live"]["met"]
    line = product.brief_lines(store, "P", head=HEAD)[0]
    assert "достигли требуемого 1" in line and "прибор сменился 1 (O-tool)" in line
    witness(store, "O-live", "broken", wid="w2")
    item = next(i for i in product.state(store, "P", head=HEAD) if i["id"] == "O-live")
    assert item["broken"] and not item["met"]


def test_witness_for_obligation_missing_from_ledger_is_reported(env):
    store, root = env
    ledger(root, {"id": "O-a", "promise": "p"})
    product.sync(store)
    witness(store, "O-update-channel", "works_live", wid="update-channel")
    lines = product.brief_lines(store, "P", head=HEAD)
    assert any("update-channel→O-update-channel" in line for line in lines)
    with store.read() as tx:
        assert tx.scalar("SELECT count(*) FROM evidence") == 0


def test_backfill_binds_old_results_once(env):
    store, root = env
    ledger(root, {"id": "O-a", "promise": "p"})
    with store.tx() as tx:
        tx.record_event(kind="witness_result", project="P", tz="witness:w1", payload={
            "id": "w1", "obligation": "O-a", "verdict": "works_live", "head": HEAD,
            "instrument_sha": "s1"})
        tx.record_event(kind="witness_result", project="P", tz="witness:w1", payload={
            "id": "w1", "obligation": "O-a", "verdict": "works_live"})  # без HEAD — не доказательство
    assert product.sync(store)["P"]["evidence"] == 1
    assert product.backfill(store, "P") == 0


def test_contract_coverage_is_deterministic(env):
    """2.2: пункт канона → O-* / declined(reason) / непокрыт; непокрытое — строка брифа."""
    store, root = env
    (root / "intent").mkdir()
    (root / "intent" / "goal.md").write_text(
        "# Цель\n\n## Результат\n\n1. Не пункт контракта.\n\n## Сценарии\n\n"
        "1. **S01 — первый.** Текст.\n2. **Второй без метки.** Текст.\n3. **S03 — третий.** Т.\n\n"
        "## Вне рамок\n\n1. Не пункт.\n", encoding="utf-8")
    ledger_data = {"obligations": [{"id": "O-a", "source": "intent/goal.md#S01", "promise": "p"},
                                   {"id": "O-b", "promise": "закрывает сценарий S01 тоже"}],
                   "declined": [{"ref": "S03", "reason": "вне продукта по решению владельца"}]}
    (root / "ToDo" / "lead" / "LEDGER.json").write_text(json.dumps(ledger_data, ensure_ascii=False),
                                                       encoding="utf-8")
    cov = product.coverage(root)
    assert [i["id"] for i in product.contract_items(root)] == ["S01", "S02", "S03"]
    assert cov["items"]["S01"]["obligations"] == ["O-a", "O-b"]
    assert cov["covered"] == ["S01"] and cov["declined"] == ["S03"] and cov["uncovered"] == ["S02"]
    product.sync(store)
    line = product.brief_lines(store, "P", head=HEAD)[0]
    assert "пунктов 3, покрыто O-* 1, отклонено 1" in line and "S02 «Второй без метки»" in line
