from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet import judgment
from dev_mcp.fleet.model import norm_path
from dev_mcp.fleet.queue import _triage_submit_reason
from dev_mcp.fleet.store import Store


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    s = Store.open(str(tmp_path / "fleet.sqlite"))
    root = tmp_path / "P"
    (root / "ToDo" / "decisions").mkdir(parents=True)
    with s.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES('P',?,1,1)", (str(root),),
                  event={"kind": "test_setup"})
    return s


def add(store: Store, tz: str, *, state: str = "ready", kind: str = "work", title: str = "t",
        spec: str = "", needs: list[str] | None = None) -> None:
    with store.tx() as tx:
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,resources,"
            "acceptance,body,created_at,updated_at) VALUES('P',?,?,?,1,?,?,'',?,'[]','[]','[]','',?,?)",
            (tz, title, state, kind, spec, json.dumps(needs or []), now(), now()),
            event={"kind": "test_setup"},
        )


def event(store: Store, kind: str, tz: str, **payload) -> None:
    with store.tx() as tx:
        tx.record_event(kind=kind, project="P", tz=tz, worker_id=payload.pop("worker_id", None), payload=payload)


def triages(store: Store) -> list[dict]:
    with store.tx() as tx:
        return [dict(r) for r in tx.query("SELECT tz,title,owns,body,state FROM tasks WHERE kind='triage'")]


def test_three_rejects_open_one_triage_owning_the_spec(store: Store) -> None:
    add(store, "TZ-1", spec="ToDo/tz/TZ-1.md")
    for i in range(2):
        event(store, "reject", "TZ-1", note=f"не так {i}")
    assert judgment.open_triages(store) == []
    event(store, "reject", "fix:TZ-1:2", note="опять")  # отказ fix — та же семья
    assert judgment.open_triages(store) == ["P/triage:1"]
    t = triages(store)[0]
    assert "subject=rejects:TZ-1" in t["body"] and "опять" in t["body"]
    assert json.loads(t["owns"])[0].endswith("/todo/tz/tz-1.md")
    assert judgment.open_triages(store) == []  # одна открытая на предмет


def test_rejects_count_again_only_after_previous_triage(store: Store) -> None:
    add(store, "TZ-1")
    for _ in range(3):
        event(store, "reject", "TZ-1")
    judgment.open_triages(store)
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE tz='triage:1'", (), event={"kind": "test_setup"})
    event(store, "reject", "TZ-1")
    assert judgment.open_triages(store) == []  # старые три уже разобраны
    event(store, "reject", "TZ-1")
    event(store, "reject", "TZ-1")
    assert judgment.open_triages(store) == ["P/triage:2"]


def test_daily_budget_per_project(store: Store) -> None:
    for n in range(1, judgment.DAILY_BUDGET + 3):
        add(store, f"TZ-{n}")
        for _ in range(3):
            event(store, "reject", f"TZ-{n}")
    assert len(judgment.open_triages(store)) == judgment.DAILY_BUDGET


def test_gate_cluster_asks_which_tasks_do_not_really_wait(store: Store) -> None:
    for n in range(1, 7):
        add(store, f"TZ-{n}", spec=f"ToDo/tz/TZ-{n}.md", needs=["TZ-90"])
    add(store, "TZ-90", state="frozen")
    data = {"clusters": [{"project": "P", "gates": ["TZ-90"], "tasks": [f"TZ-{n}" for n in range(1, 7)]}],
            "items": [{"project": "P", "tz": "TZ-90", "reason": "ждёт телефон", "gate": True,
                       "needed_for": 6, "age_min": 60, "title": "x"}]}
    assert judgment.open_triages(store, lambda: data) == ["P/triage:1"]
    t = triages(store)[0]
    assert "subject=gates:TZ-90" in t["body"] and "ждёт телефон" in t["body"]
    assert len(json.loads(t["owns"])) == 1  # один файл решения, а не ТЗ всех задач
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE tz='triage:1'", (), event={"kind": "test_setup"})
    assert judgment.open_triages(store, lambda: data) == []  # пауза после разобранного


def test_old_owner_gate_gets_decision_triage(store: Store) -> None:
    add(store, "TZ-90", state="frozen", title="ворота")
    item = {"project": "P", "tz": "TZ-90", "reason": "нужен ответ", "gate": True, "needed_for": 3,
            "age_min": 30 * 60, "title": "ворота"}
    assert judgment.open_triages(store, lambda: {"clusters": [], "items": [item]}) == ["P/triage:1"]
    t = triages(store)[0]
    assert "ToDo/decisions/TZ-90.md" in t["body"]
    assert json.loads(t["owns"])[0].endswith("/todo/decisions/tz-90.md")
    item["age_min"] = 60
    with store.tx() as tx:
        tx.change("DELETE FROM tasks WHERE kind='triage'", (), event={"kind": "test_setup"})
        tx.change("DELETE FROM events WHERE kind='triage_open'", (), event={"kind": "test_setup"})
    assert judgment.open_triages(store, lambda: {"clusters": [], "items": [item]}) == []  # молодые ворота ждут


def test_accepted_watchdog_triage_is_not_reopened_as_invalid_owner_decision(
    store: Store, tmp_path: Path,
) -> None:
    # A watchdog report is observability, not an owner YAML action proposal.
    tz = "watch:7dccfcd044ea"
    add(store, tz, kind="triage", state="accepted")
    path = tmp_path / "P" / "ToDo" / "decisions" / "watchdog-7dccfcd044ea.md"
    path.write_text(
        "# metrics_mismatch\n\n**Решение: observe/observability, не recovery.**\n",
        encoding="utf-8",
    )
    with store.tx() as tx:
        tx.record_event(kind="write", project="P", tz=tz, worker_id="W1",
                        path_norm=norm_path(str(path)))
    out = judgment.apply_accepted(store)
    with store.read() as tx:
        state = tx.scalar("SELECT state FROM tasks WHERE project='P' AND tz=?", (tz,))
        errors = tx.scalar(
            "SELECT COUNT(*) FROM events WHERE project='P' AND tz=? "
            "AND kind='triage_apply_error'", (tz,),
        )
        applied = tx.scalar(
            "SELECT COUNT(*) FROM events WHERE project='P' AND tz=? "
            "AND kind='triage_applied'", (tz,),
        )
    assert state == "accepted"
    assert errors == 0
    assert applied == 1
    assert out[0].get("status") != "repair"
    assert judgment.apply_accepted(store) == []


def test_decision_file_name_has_no_colon_for_fix_tasks(store: Store) -> None:
    # 09.10.2026: `ToDo/decisions/fix:TZ-441.md` страж отклонил как поток NTFS — суждение замерзло.
    add(store, "fix:TZ-441", state="frozen", title="ворота")
    item = {"project": "P", "tz": "fix:TZ-441", "reason": "r", "gate": True, "needed_for": 3,
            "age_min": 30 * 60, "title": "ворота"}
    assert judgment.open_triages(store, lambda: {"clusters": [], "items": [item]}) == ["P/triage:1"]
    t = triages(store)[0]
    owns = json.loads(t["owns"])[0]
    assert ":" not in owns.split("/todo/decisions/")[1]
    assert "ToDo/decisions/fix__TZ-441.md" in t["body"] and "tz: fix:TZ-441" in t["body"]


def legacy_decision_fixture(store: Store, tmp_path: Path, *, state: str = "taken",
                            claim: bool = True) -> tuple[str, str, str, int | None]:
    """A legacy triage uses an NTFS ADS basename; no real project files touched."""
    add(store, "triage:9", kind="triage", state=state)
    base = tmp_path / "P" / "ToDo" / "decisions"
    old = norm_path(str(base / "fix:TZ-137:2.md"))
    new = norm_path(str(base / "fix__TZ-137__2.md"))
    body = ("[fleet-triage] subject=decide:fix:TZ-137:2\n"
            "Запиши решение в `ToDo/decisions/fix:TZ-137:2.md`\n"
            "```yaml\ntz: fix:TZ-137:2\naction: park\n```")
    claim_id = None
    with store.tx() as tx:
        tx.change(
            "UPDATE tasks SET body=?,owns=? WHERE project='P' AND tz='triage:9'",
            (body, json.dumps([old])),
            event={"kind": "test_setup"},
        )
        if claim:
            tx.change("INSERT INTO workers(id,first_seen,last_seen) VALUES('W1',?,?)",
                      (now(), now()), event={"kind": "test_setup"})
            tx.change(
                "INSERT INTO claims(project,tz,worker_id,state,started_at) "
                "VALUES('P','triage:9','W1','active',?)",
                (now(),), event={"kind": "test_setup"},
            )
            claim_id = int(tx.scalar("SELECT MAX(id) FROM claims"))
            tx.change("INSERT INTO claim_paths(claim_id,path_norm) VALUES(?,?)",
                      (claim_id, old), event={"kind": "test_setup"})
    return old, new, body, claim_id


def test_legacy_decision_migration_changes_owns_body_and_active_claim_once(
    store: Store, tmp_path: Path,
) -> None:
    old, new, body, claim_id = legacy_decision_fixture(store, tmp_path)
    add(store, "triage:10", kind="triage", title="unrelated")
    with store.read() as tx:
        unrelated_before = dict(tx.one("SELECT * FROM tasks WHERE tz='triage:10'"))
    assert judgment.open_triages(store, lambda: {"clusters": [], "items": []}) == []
    with store.read() as tx:
        row = tx.one("SELECT tz,owns,body FROM tasks WHERE tz='triage:9'")
        assert row["tz"] == "triage:9"
        assert json.loads(row["owns"]) == [new]
        assert "ToDo/decisions/fix__TZ-137__2.md" in row["body"]
        assert "tz: fix:TZ-137:2" in row["body"]
        assert body != row["body"] and old not in json.loads(row["owns"])
        assert tx.scalar("SELECT path_norm FROM claim_paths WHERE claim_id=?", (claim_id,)) == new
        assert dict(tx.one("SELECT * FROM tasks WHERE tz='triage:10'")) == unrelated_before
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='triage_decision_path_migrate'") == 1
    assert judgment.migrate_legacy_decision_paths(store) == []
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='triage_decision_path_migrate'") == 1


def test_legacy_decision_migration_repairs_frozen_without_active_claim(
    store: Store, tmp_path: Path,
) -> None:
    _, new, _, _ = legacy_decision_fixture(store, tmp_path, state="frozen", claim=False)
    assert judgment.migrate_legacy_decision_paths(store) == ["P/triage:9"]
    with store.read() as tx:
        row = tx.one("SELECT owns,state FROM tasks WHERE tz='triage:9'")
        assert row["state"] == "frozen" and json.loads(row["owns"]) == [new]


def test_legacy_decision_migration_blocks_foreign_active_claim(
    store: Store, tmp_path: Path,
) -> None:
    old, new, body, claim_id = legacy_decision_fixture(store, tmp_path)
    add(store, "triage:10", kind="triage")
    with store.tx() as tx:
        tx.change("INSERT INTO workers(id,first_seen,last_seen) VALUES('W2',?,?)",
                  (now(), now()), event={"kind": "test_setup"})
        tx.change(
            "INSERT INTO claims(project,tz,worker_id,state,started_at) "
            "VALUES('P','triage:10','W2','active',?)",
            (now(),), event={"kind": "test_setup"},
        )
        foreign_id = int(tx.scalar("SELECT MAX(id) FROM claims"))
        tx.change("INSERT INTO claim_paths(claim_id,path_norm) VALUES(?,?)",
                  (foreign_id, new), event={"kind": "test_setup"})
    assert judgment.migrate_legacy_decision_paths(store) == []
    with store.read() as tx:
        row = tx.one("SELECT owns,body FROM tasks WHERE tz='triage:9'")
        assert json.loads(row["owns"]) == [old] and row["body"] == body
        assert tx.scalar("SELECT path_norm FROM claim_paths WHERE claim_id=?", (claim_id,)) == old
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='triage_decision_path_migrate'") == 0


@pytest.mark.parametrize("change", ["non_decide", "ambiguous_owns", "accepted"])
def test_legacy_decision_migration_skips_nonmatching_triage(
    store: Store, tmp_path: Path, change: str,
) -> None:
    old, new, body, _ = legacy_decision_fixture(store, tmp_path, claim=False)
    with store.tx() as tx:
        if change == "non_decide":
            tx.change("UPDATE tasks SET body=? WHERE tz='triage:9'",
                      (body.replace("subject=decide:", "subject=rejects:"),),
                      event={"kind": "test_setup"})
        elif change == "ambiguous_owns":
            tx.change("UPDATE tasks SET owns=? WHERE tz='triage:9'",
                      (json.dumps([old, new]),), event={"kind": "test_setup"})
        else:
            tx.change("UPDATE tasks SET state='accepted' WHERE tz='triage:9'",
                      (), event={"kind": "test_setup"})
    with store.read() as tx:
        before = dict(tx.one("SELECT * FROM tasks WHERE tz='triage:9'"))
    assert judgment.migrate_legacy_decision_paths(store) == []
    with store.read() as tx:
        assert dict(tx.one("SELECT * FROM tasks WHERE tz='triage:9'")) == before


def write_decision(store: Store, tmp_path: Path, tz: str, action: str, why: str = "препятствие ушло") -> None:
    path = tmp_path / "P" / "ToDo" / "decisions" / f"{tz}.md"
    path.write_text(f"# Решение\n```yaml\ntz: {tz}\naction: {action}\nwhy: {why}\n```\n", encoding="utf-8")
    add(store, "triage:1", kind="triage", state="accepted")
    with store.tx() as tx:
        tx.record_event(kind="write", project="P", tz="triage:1", worker_id="W1", path_norm=norm_path(str(path)))


def test_accepted_decision_becomes_proposal_and_default_applies_after_deadline(store: Store, tmp_path: Path) -> None:
    add(store, "TZ-90", state="frozen", title="перепроверить стенд")
    write_decision(store, tmp_path, "TZ-90", "reactivate")
    done = judgment.apply_accepted(store)
    assert done[0]["proposals"] == ["TZ-90: reactivate"]
    assert judgment.apply_accepted(store) == []  # применяется один раз
    later = datetime.now(timezone.utc)
    assert judgment.apply_defaults(store) == ["P/TZ-90: reactivate"]  # обратимое — сразу, без срока
    with store.tx() as tx:
        assert tx.scalar("SELECT state FROM tasks WHERE tz='TZ-90'") == "ready"
    assert "снова frozen" in judgment.undo_default(store, "P", "TZ-90")
    with store.tx() as tx:
        assert tx.scalar("SELECT state FROM tasks WHERE tz='TZ-90'") == "frozen"
    with pytest.raises(ValueError):
        judgment.undo_default(store, "P", "TZ-90")
    assert judgment.apply_defaults(store, now=later) == []  # откаченное не применяется снова


def test_money_or_hardware_never_applies_by_default(store: Store, tmp_path: Path) -> None:
    add(store, "TZ-175", state="frozen", title="Provision ingress after owner approval")
    event(store, "block", "TZ-175", reason="validate-prespend: 11.99 USD / 1 month")
    write_decision(store, tmp_path, "TZ-175", "reactivate", why="агент сам всё сделает")
    assert judgment.apply_accepted(store)[0]["proposals"] == ["TZ-175: reactivate (ждёт владельца)"]
    # Просьба без фразы для владельца — переспросить сразу, несмотря на паузу после прошлой triage.
    item = {"project": "P", "tz": "TZ-175", "reason": "трата", "gate": True, "needed_for": 2,
            "age_min": 600, "title": "ingress"}
    with store.tx() as tx:
        tx.record_event(kind="triage_open", project="P", tz="triage:1", payload={"subject": "decide:TZ-175"})
    assert judgment.open_triages(store, lambda: {"clusters": [], "items": [item]}) == ["P/triage:2"]
    later = datetime.now(timezone.utc) + timedelta(days=30)
    assert judgment.apply_defaults(store, now=later) == []


def test_owner_move_voids_proposal(store: Store, tmp_path: Path) -> None:
    add(store, "TZ-90", state="frozen")
    write_decision(store, tmp_path, "TZ-90", "keep")  # ждёт владельца
    judgment.apply_accepted(store)
    event(store, "thaw", "TZ-90")
    later = datetime.now(timezone.utc) + timedelta(days=3)
    assert judgment.apply_defaults(store, now=later) == []
    with store.tx() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='proposal_void'") == 1


def test_accepted_triage_resyncs_edited_specs_except_taken(store: Store, tmp_path: Path) -> None:
    tz_dir = tmp_path / "P" / "ToDo" / "tz"
    tz_dir.mkdir(parents=True)
    for n, state in ((1, "ready"), (2, "taken")):
        (tz_dir / f"TZ-{n}.md").write_text(
            f"```yaml\ntz: TZ-{n}\nowns: [a{n}.py]\nacceptance: [py -3 -c pass]\n```\n# TZ-{n}\n", encoding="utf-8")
        add(store, f"TZ-{n}", state=state)
    add(store, "triage:1", kind="triage", state="accepted")
    with store.tx() as tx:
        for n in (1, 2):
            tx.record_event(kind="write", project="P", tz="triage:1", worker_id="W1",
                            path_norm=norm_path(str(tz_dir / f"TZ-{n}.md")))
    calls = []
    out = judgment.apply_accepted(store, sync=lambda s, p, r, tzs: calls.append(list(tzs)) or {})
    assert calls == [["TZ-1"]]
    assert out[0]["skipped_taken"] == ["TZ-2"]


def test_triage_submit_needs_files_or_request(store: Store) -> None:
    add(store, "triage:1", kind="triage", state="taken")
    claim = {"project": "P", "tz": "triage:1", "started_at": now()}
    with store.tx() as tx:
        assert "правка файлов" in _triage_submit_reason(tx, "W1", claim)
    with store.tx() as tx:
        tx.record_event(kind="write", project="P", tz="triage:1", worker_id="W1", path_norm="c:/p/x.md")
    with store.tx() as tx:
        assert _triage_submit_reason(tx, "W1", claim) == ""
        add_claim = {"project": "P", "tz": "TZ-1", "started_at": now()}
        assert _triage_submit_reason(tx, "W1", add_claim) == ""  # не triage — не наше правило


def test_irreversible_words() -> None:
    assert judgment.irreversible("нужна SIM-карта", "")
    assert judgment.irreversible("11.99 USD")
    assert not judgment.irreversible("перепроверить стенд после правки теста")


def test_park_is_not_an_owner_ask_and_is_rechecked_later(store: Store, tmp_path: Path) -> None:
    add(store, "TZ-175", state="frozen", title="ingress")
    write_decision(store, tmp_path, "TZ-175", "park", why="если проверка снова покажет блок адреса — нужна трата")
    assert judgment.apply_accepted(store)[0]["proposals"] == ["TZ-175: park"]
    assert judgment.apply_defaults(store) == []  # park ничего не меняет в очереди
    item = {"project": "P", "tz": "TZ-175", "reason": "r", "gate": True, "needed_for": 1, "age_min": 600, "title": "i"}
    assert judgment.open_triages(store, lambda: {"clusters": [], "items": [item]}) == []  # рано
    later = datetime.now(timezone.utc) + timedelta(hours=judgment.GATE_COOLDOWN_H + 1)
    assert judgment.open_triages(store, lambda: {"clusters": [], "items": [item]}, now=later) == ["P/triage:2"]


def test_big_gate_cluster_goes_before_rejects_when_budget_is_short(store: Store, monkeypatch) -> None:
    monkeypatch.setattr(judgment, "DAILY_BUDGET", 1)
    add(store, "TZ-1")
    for _ in range(3):
        event(store, "reject", "TZ-1")
    for n in range(10, 16):
        add(store, f"TZ-{n}", needs=["TZ-61"])
    data = {"clusters": [{"project": "P", "gates": ["TZ-61"], "tasks": [f"TZ-{n}" for n in range(10, 16)]}],
            "items": []}
    opened = judgment.open_triages(store, lambda: data)
    assert len(opened) == 1 and "gates:TZ-61" in triages(store)[0]["body"]


def test_conditional_owner_ask_is_reasked_as_park(store: Store, tmp_path: Path) -> None:
    add(store, "TZ-175", state="frozen", title="ingress")
    path = tmp_path / "P" / "ToDo" / "decisions" / "TZ-175.md"
    path.write_text(chr(10).join(["```yaml", "tz: TZ-175", "action: keep", "why: w",
                                   "owner_needs: Если проверка покажет блок — оплати $12", "```", ""]),
                    encoding="utf-8")
    add(store, "triage:1", kind="triage", state="accepted")
    with store.tx() as tx:
        tx.record_event(kind="write", project="P", tz="triage:1", worker_id="W1", path_norm=norm_path(str(path)))
        tx.record_event(kind="triage_open", project="P", tz="triage:1", payload={"subject": "decide:TZ-175"})
    judgment.apply_accepted(store)
    item = {"project": "P", "tz": "TZ-175", "reason": "r", "gate": True, "needed_for": 1, "age_min": 600, "title": "i"}
    assert judgment.open_triages(store, lambda: {"clusters": [], "items": [item]}) == ["P/triage:2"]


def test_gate_triage_owns_one_decision_file_and_server_unlinks_needs(store: Store, tmp_path: Path) -> None:
    tz_dir = tmp_path / "P" / "ToDo" / "tz"
    tz_dir.mkdir(parents=True)
    for n in range(1, 7):
        (tz_dir / f"TZ-{n}.md").write_text(
            "```yaml\n" f"tz: TZ-{n}\nowns: [a{n}.py]\nneeds: [TZ-90, TZ-5#contract]\nacceptance: [py -3 -c pass]\n"
            "```\n" f"# TZ-{n}\n", encoding="utf-8")
        add(store, f"TZ-{n}", spec=f"ToDo/tz/TZ-{n}.md", needs=["TZ-90"])
    add(store, "TZ-90", state="frozen")
    data = {"clusters": [{"project": "P", "gates": ["TZ-90"], "tasks": [f"TZ-{n}" for n in range(1, 7)]}], "items": []}
    assert judgment.open_triages(store, lambda: data) == ["P/triage:1"]
    owns = json.loads(triages(store)[0]["owns"])
    assert len(owns) == 1 and owns[0].endswith("/todo/decisions/gates-tz-90.md")  # не 6 файлов ТЗ

    decision = tmp_path / "P" / "ToDo" / "decisions" / "gates-TZ-90.md"
    decision.write_text("```yaml\nunlink:\n  TZ-1: [TZ-90]\n  TZ-2: [TZ-90]\nwhy: не зависят\n```\n", encoding="utf-8")
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE tz='triage:1'", (), event={"kind": "test_setup"})
        tx.change("UPDATE tasks SET state='taken' WHERE tz='TZ-2'", (), event={"kind": "test_setup"})
        tx.record_event(kind="write", project="P", tz="triage:1", worker_id="W1", path_norm=norm_path(str(decision)))
    calls = []
    out = judgment.apply_accepted(store, sync=lambda s, p, r, tzs: calls.append(list(tzs)) or {})
    assert out[0]["unlinked"] == ["TZ-1"] and out[0]["skipped_taken"] == ["TZ-2"]
    assert "needs: [TZ-5#contract]" in (tz_dir / "TZ-1.md").read_text(encoding="utf-8")
    assert "needs: [TZ-90, TZ-5#contract]" in (tz_dir / "TZ-2.md").read_text(encoding="utf-8")
    assert calls == [["TZ-1"]]
    assert judgment.resync_skipped(store, sync=lambda s, p, r, tzs: calls.append(list(tzs)) or {}) == []  # TZ-2 ещё взята
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='ready' WHERE tz='TZ-2'", (), event={"kind": "test_setup"})
    assert judgment.resync_skipped(store, sync=lambda s, p, r, tzs: calls.append(list(tzs)) or {}) == ["P/triage:1"]
    assert calls[-1] == ["TZ-2"]
    assert "needs: [TZ-5#contract]" in (tz_dir / "TZ-2.md").read_text(encoding="utf-8")  # unlink догнал
    assert judgment.resync_skipped(store, sync=lambda s, p, r, tzs: calls.append(list(tzs)) or {}) == []  # один раз


def unlink_case(store: Store, tmp_path: Path, header: str, *, tz: str = "TZ-106") -> tuple[Path, Path]:
    """An isolated spec and gates decision; neither real project files nor the real DB change."""
    directory = tmp_path / "P" / "ToDo" / "tz"
    directory.mkdir(parents=True, exist_ok=True)
    spec = directory / f"{tz}.md"
    spec.write_text("```yaml\n" + header + "```\n# Body\nneeds: [TZ-186] is prose only\n", encoding="utf-8")
    add(store, tz, spec=f"ToDo/tz/{tz}.md")
    decision = tmp_path / "P" / "ToDo" / "decisions" / "gates-TZ-186.md"
    decision.write_text(f"```yaml\nunlink:\n  {tz}: [TZ-186]\nwhy: separate gate\n```\n", encoding="utf-8")
    return spec, decision


def test_unlink_multiline_yaml_preserves_other_dependencies_and_comments(store: Store, tmp_path: Path) -> None:
    spec, decision = unlink_case(
        store, tmp_path,
        "tz: TZ-106\n"
        "needs:\n"
        "  - TZ-100\n"
        "  - TZ-186  # remove only this dependency\n"
        "  - TZ-103  # keep signed-policy gate\n"
        "  - TZ-104\n"
        "owns: [a.py]\n",
    )
    edited, skipped, errors = judgment._apply_unlink(store, "P", str(tmp_path / "P"), decision, set())
    assert (edited, skipped, errors) == (["TZ-106"], [], [])
    text = spec.read_text(encoding="utf-8")
    header, body = text.split("```", 2)[1:]
    assert "  - TZ-100\n" in header
    assert "  - TZ-103  # keep signed-policy gate\n" in header
    assert "  - TZ-104\n" in header
    assert "TZ-186" not in header
    assert "owns: [a.py]\n" in header
    assert "needs: [TZ-186] is prose only" in body


def test_unlink_last_block_gate_writes_valid_empty_sequence_and_is_idempotent(
    store: Store, tmp_path: Path,
) -> None:
    # Removing the final block item must not leave "needs:" as YAML null.
    # Keep the header comment and Markdown body byte-for-byte otherwise.
    spec, decision = unlink_case(
        store, tmp_path,
        "tz: TZ-106\nneeds:  # only owner gate\n  - TZ-186\n"
        "owns: [a.py]\n",
    )
    assert judgment._apply_unlink(store, "P", str(tmp_path / "P"), decision, set()) == (
        ["TZ-106"], [], [],
    )
    updated_bytes = spec.read_bytes()
    updated = spec.read_text(encoding="utf-8")  # normalize CRLF on Windows
    assert "needs: []  # only owner gate\n" in updated
    assert "owns: [a.py]\n" in updated
    assert "# Body\nneeds: [TZ-186] is prose only\n" in updated
    assert judgment._apply_unlink(store, "P", str(tmp_path / "P"), decision, set()) == (
        [], [], [],
    )
    assert spec.read_bytes() == updated_bytes


def test_unlink_inline_yaml_keeps_remaining_gates_and_comment(store: Store, tmp_path: Path) -> None:
    spec, decision = unlink_case(
        store, tmp_path,
        "tz: TZ-106\nneeds: [TZ-186, TZ-78, TZ-81]  # current live gates\n"
        "acceptance: [py -3 -m pytest tests/contracts/test_sample.py -q]\n",
    )
    edited, skipped, errors = judgment._apply_unlink(store, "P", str(tmp_path / "P"), decision, set())
    assert (edited, skipped, errors) == (["TZ-106"], [], [])
    updated = spec.read_text(encoding="utf-8")
    assert "needs: [TZ-78, TZ-81]  # current live gates" in updated
    assert "acceptance: [py -3 -m pytest tests/contracts/test_sample.py -q]" in updated


@pytest.mark.parametrize("header", [
    "tz: TZ-106\nneeds:\n  - TZ-100\n  - TZ-103 # keep\n",
    "tz: TZ-106\nneeds: [TZ-100, TZ-103]  # no TZ-186 in dependency values\n",
    "tz: TZ-106\nowns: [a.py]\n",  # no needs key is an already-unlinked spec
])
def test_unlink_missing_gate_or_missing_needs_is_noop(
    store: Store, tmp_path: Path, header: str,
) -> None:
    spec, decision = unlink_case(store, tmp_path, header)
    original = spec.read_bytes()
    assert judgment._apply_unlink(store, "P", str(tmp_path / "P"), decision, set()) == ([], [], [])
    assert spec.read_bytes() == original


def test_accepted_gate_unlink_noop_marks_applied_without_triage_requeue(store: Store, tmp_path: Path) -> None:
    tz_dir = tmp_path / "P" / "ToDo" / "tz"
    tz_dir.mkdir(parents=True)
    originals = {}
    for tz, head in (
        ("TZ-106", "tz: TZ-106\nneeds:\n  - TZ-100\n  - TZ-103  # signed policy\n"),
        ("TZ-25", "tz: TZ-25\nneeds: [TZ-78]  # current M4 gate\n"),
        ("TZ-82", "tz: TZ-82\nowns: [capture.json]\n"),
    ):
        path = tz_dir / f"{tz}.md"
        path.write_text("```yaml\n" + head + "```\n# Scope unchanged\n", encoding="utf-8")
        add(store, tz, spec=f"ToDo/tz/{tz}.md")
        originals[tz] = path.read_bytes()
    decision = tmp_path / "P" / "ToDo" / "decisions" / "gates-TZ-186.md"
    decision.write_text(
        "```yaml\nunlink:\n  TZ-106: [TZ-186]\n  TZ-25: [TZ-186]\n"
        "  TZ-82: [TZ-186]\nwhy: self-contained successors\n```\n",
        encoding="utf-8",
    )
    add(store, "triage:1", kind="triage", state="accepted")
    with store.tx() as tx:
        tx.record_event(kind="write", project="P", tz="triage:1", worker_id="W1",
                        path_norm=norm_path(str(decision)))
    calls: list[list[str]] = []
    sync = lambda _s, _p, _r, ts: calls.append(list(ts)) or {}
    result = judgment.apply_accepted(store, sync=sync)
    assert len(result) == 1
    assert result[0].get("unlinked") == []
    assert result[0].get("errors") is None
    assert result[0].get("status") != "repair"
    assert calls == []
    with store.read() as tx:
        assert tx.scalar("SELECT state FROM tasks WHERE tz='triage:1'") == "accepted"
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='triage_applied' AND tz='triage:1'") == 1
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='triage_apply_error' AND tz='triage:1'") == 0
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='triage_apply_retry' AND tz='triage:1'") == 0
    assert judgment.apply_accepted(store, sync=sync) == []
    assert calls == []
    for tz, original in originals.items():
        assert (tz_dir / f"{tz}.md").read_bytes() == original


def _frozen_nova_gate_case(store: Store, tmp_path: Path) -> tuple[Path, Path]:
    """Model the accepted repair followed by three failed live apply attempts."""
    root = tmp_path / "P"
    tz_dir = root / "ToDo" / "tz"
    tz_dir.mkdir(parents=True, exist_ok=True)
    capture = tz_dir / "TZ-82.md"
    capture.write_text(
        "```yaml\n"
        "tz: TZ-82\n"
        "needs:\n"
        "  - TZ-76\n"
        "  - TZ-78\n"
        "  - TZ-81\n"
        "owns: [ToDo/fleet/results/TZ-82]\n"
        "resources: [phone]\n"
        "obligations:\n"
        "  - explicit-participant-consent\n"
        "  - usb-disconnected-autonomy\n"
        "  - fresh-post-baseline-upload\n"
        "acceptance:\n"
        "  - py -3 ops/agents/fleet_plan.py result TZ-82 --require-verified\n"
        "```\n# Autonomous capture must retain its own evidence\n",
        encoding="utf-8",
    )
    m4 = tz_dir / "TZ-25.md"
    m4.write_text("```yaml\ntz: TZ-25\nneeds: [TZ-78]\n```\n# M4 live gate\n", encoding="utf-8")
    add(store, "TZ-82", spec="ToDo/tz/TZ-82.md", needs=["TZ-76", "TZ-78", "TZ-81"])
    add(store, "TZ-25", spec="ToDo/tz/TZ-25.md", needs=["TZ-78"])
    add(store, "TZ-78", state="frozen")
    add(store, "triage:29", kind="triage", state="frozen")
    add(store, "fix:triage:29", kind="fix", state="accepted")
    decision = root / "ToDo" / "decisions" / "gates-TZ-78.md"
    decision.write_text(
        "```yaml\nunlink:\n  TZ-82: [TZ-78]\nwhy: independent field capture\n```\n",
        encoding="utf-8",
    )
    with store.tx() as tx:
        tx.change(
            "UPDATE tasks SET resources=?, acceptance=? WHERE project='P' AND tz='TZ-82'",
            (json.dumps(["phone"]), json.dumps([
                "py -3 ops/agents/fleet_plan.py result TZ-82 --require-verified",
            ])),
            event={"kind": "test_setup"},
        )
        for tz in ("triage:29", "fix:triage:29"):
            tx.record_event(kind="write", project="P", tz=tz, path_norm=norm_path(str(decision)))
        tx.record_event(kind="accept", project="P", tz="fix:triage:29")
        for _ in range(2):
            tx.record_event(kind="triage_apply_retry", project="P", tz="triage:29",
                            payload={"errors": ["old block-list parser failed"]})
        tx.record_event(kind="triage_apply_error", project="P", tz="triage:29",
                        payload={"errors": ["old block-list parser failed"]})
        tx.record_event(kind="block", project="P", tz="triage:29",
                        payload={"reason": "accepted repair needs guarded replay"})
    return capture, m4


def test_frozen_accepted_nova_unlink_only_one_edge_and_no_false_green(
    store: Store, tmp_path: Path,
) -> None:
    capture, m4 = _frozen_nova_gate_case(store, tmp_path)
    m4_original = m4.read_bytes()
    result = judgment.apply_accepted(store)
    assert len(result) == 1, result
    assert result[0]["tz"] == "triage:29"
    assert result[0].get("recovered_frozen") is True
    assert result[0].get("errors") is None, result
    assert result[0]["unlinked"] == ["TZ-82"]
    changed = capture.read_text(encoding="utf-8")
    assert "needs:\n  - TZ-76\n  - TZ-81\n" in changed
    assert "  - TZ-78\n" not in changed
    assert "resources: [phone]" in changed
    for requirement in ("explicit-participant-consent", "usb-disconnected-autonomy",
                        "fresh-post-baseline-upload", "--require-verified"):
        assert requirement in changed
    assert m4.read_bytes() == m4_original
    with store.read() as tx:
        assert json.loads(tx.one("SELECT needs FROM tasks WHERE tz='TZ-82'")["needs"]) == ["TZ-76", "TZ-81"]
        assert json.loads(tx.one("SELECT needs FROM tasks WHERE tz='TZ-25'")["needs"]) == ["TZ-78"]
        assert tx.one("SELECT state FROM tasks WHERE tz='TZ-82'")["state"] == "ready"
        assert tx.one("SELECT state FROM tasks WHERE tz='TZ-78'")["state"] == "frozen"
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='triage_applied' AND tz='triage:29'") == 1
    capture_bytes = capture.read_bytes()
    assert judgment.apply_accepted(store) == []
    assert capture.read_bytes() == capture_bytes
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='triage_applied' AND tz='triage:29'") == 1


@pytest.mark.parametrize("veto", ["fix_not_accepted", "late_reject", "late_freeze", "late_gates_write", "late_foreign_write", "wrong_reviewed_file"])
def test_frozen_unlink_requires_fresh_independent_authority(
    store: Store, tmp_path: Path, veto: str,
) -> None:
    capture, _ = _frozen_nova_gate_case(store, tmp_path)
    original = capture.read_bytes()
    with store.tx() as tx:
        if veto == "fix_not_accepted":
            tx.change("UPDATE tasks SET state='rejected' WHERE tz='fix:triage:29'",
                      (), event={"kind": "test_setup"})
        elif veto == "wrong_reviewed_file":
            tx.change("UPDATE events SET path_norm=? WHERE project='P' AND tz='fix:triage:29' AND kind='write'",
                      (norm_path(str(tmp_path / "P" / "ToDo" / "decisions" / "gates-TZ-79.md")),),
                      event={"kind": "test_setup"})
        elif veto in ("late_gates_write", "late_foreign_write"):
            tx.record_event(kind="write", project="P",
                            tz="triage:29" if veto == "late_gates_write" else "TZ-25",
                            path_norm=norm_path(str(tmp_path / "P" / "ToDo" / "decisions" / "gates-TZ-78.md")))
        else:
            tx.record_event(kind="reject" if veto == "late_reject" else "freeze",
                            project="P", tz="triage:29")
    assert judgment.apply_accepted(store) == []
    assert capture.read_bytes() == original
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='triage_applied'") == 0


def test_hardware_words_no_longer_wait_for_owner() -> None:
    # 18.10: у VPN каждая задача про телефон/устройство — это ресурс очереди, не решение владельца.
    assert not judgment.irreversible("нужен телефон TECNO по ADB и внешний vSwitch на втором ПК")
    assert not judgment.irreversible("device session", "$env:PATH")
    assert judgment.irreversible("оплатить VPS на месяц")
    assert judgment.irreversible("опубликовать в store listing")
