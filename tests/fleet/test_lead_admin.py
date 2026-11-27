"""Полномочия ведущего в своём проекте и обязанности в брифе (06.11.2026)."""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet import lead_admin, project_config  # noqa: E402
from dev_mcp.fleet.store import Store  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task(store: Store, project: str, tz: str, state: str, owns: list[str] | None = None) -> None:
    with store.tx() as tx:
        tx.change("INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,"
                  "resources,acceptance,author_worker,body,created_at,updated_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (project, tz, tz, state, 5, "work", "", "", "[]", json.dumps(owns or []), "[]", "[]",
                   None, "b", _now(), _now()), event={"kind": "test_setup"})


@pytest.fixture()
def env(tmp_path: Path):
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    root = tmp_path / "Proj"
    (root / "ToDo" / "lead").mkdir(parents=True)
    with store.tx() as tx:
        for name, r in (("P", root), ("Q", tmp_path / "Other")):
            tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)", (name, str(r)),
                      event={"kind": "test_setup"})
        for w in ("W1", "W2"):
            tx.change("INSERT INTO workers(id,first_seen,last_seen) VALUES(?,?,?)", (w, _now(), _now()),
                      event={"kind": "test_setup"})
    _task(store, "P", "TZ-LEAD", "taken")
    with store.tx() as tx:
        tx.change("INSERT INTO claims(project,tz,worker_id,state,started_at) VALUES('P','TZ-LEAD','W1','active',?)",
                  (_now(),), event={"kind": "test_setup"})
    return store, root


def test_only_lead_holder_acts_and_only_in_own_project(env):
    store, _ = env
    _task(store, "P", "review:TZ-266:2", "frozen")
    _task(store, "Q", "TZ-9", "frozen")
    with pytest.raises(ValueError, match="держатель"):
        lead_admin.act(store, "W2", "reactivate", {"tz": "review:TZ-266:2", "reason": "причина снята"})
    with pytest.raises(ValueError, match="не найдена"):
        lead_admin.act(store, "W1", "reactivate", {"tz": "TZ-9", "reason": "чужой проект тут"})
    with pytest.raises(ValueError, match="устав"):
        lead_admin.act(store, "W1", "freeze", {"tz": "fix:TZ-LEAD:3", "reason": "нельзя свой устав"})
    out = lead_admin.act(store, "W1", "reactivate", {"tz": "review:TZ-266:2", "reason": "тест исправлен веху"})
    assert "frozen → ready" in out
    with store.read() as tx:
        assert tx.one("SELECT state FROM tasks WHERE tz='review:TZ-266:2'")["state"] == "ready"
        assert tx.one("SELECT 1 FROM events WHERE kind='lead_reactivate'") is not None


def test_issue_subtask_respects_owner_canon_and_overlap(env, monkeypatch):
    store, root = env
    # The command is validated structurally; the Windows `py` launcher is
    # optional on a Python 3.13-only CI host.
    original_launcher = lead_admin._has_launcher
    monkeypatch.setattr(lead_admin, "_has_launcher",
                        lambda command, base: True if command.startswith("py -3 ")
                        else original_launcher(command, base))
    body = "Цель: сторож связности. Границы: только guard. Критерий: тест падает до правки и проходит." * 2
    with pytest.raises(ValueError, match="канон"):
        lead_admin.act(store, "W1", "issue", {"title": "x", "body": body, "owns": ["product/a.md"],
                                              "acceptance": ["py -3 -m pytest -q"]})
    out = lead_admin.act(store, "W1", "issue", {"title": "guard", "body": body, "owns": ["client/guard"],
                                                "acceptance": ["py -3 -m pytest -q tests/guard"]})
    assert "sub:LEAD-1" in out
    with pytest.raises(ValueError, match="пересекается"):
        lead_admin.act(store, "W1", "issue", {"title": "y", "body": body, "owns": ["client/guard/x.py"],
                                              "acceptance": ["py -3 -m pytest -q"]})
    with store.read() as tx:
        row = tx.one("SELECT state,kind,owns FROM tasks WHERE tz='sub:LEAD-1'")
    assert row["state"] == "ready" and row["kind"] == "work" and "client/guard" in row["owns"]


def test_issue_keeps_descriptive_review_criteria_out_of_commands(env):
    store, _ = env
    body = "Цель — независимая проверка результата. Зона — папка отчёта. Прочитать вход и сверить."
    criterion = "Датированный независимый вердикт с точной ревизией и отрицательным контролем."
    params = {"title": "review artifact", "body": body, "owns": ["ToDo/lead/reviews/one"]}
    with pytest.raises(ValueError, match="исполняемую команду"):
        lead_admin.act(store, "W1", "issue", {**params, "acceptance": [criterion]})
    lead_admin.act(store, "W1", "issue", {**params, "criteria": [criterion]})
    with store.read() as tx:
        row = tx.one("SELECT tz,body,acceptance FROM tasks WHERE tz='sub:LEAD-1'")
    assert json.loads(row["acceptance"]) == []
    assert lead_admin.review_criteria(row["tz"], row["body"]) == [criterion]


def test_issue_parallel_lanes_are_bounded_by_owns_and_resources_not_global_wip(env):
    store, _ = env
    body = "Реальный независимый пользовательский результат, критерии и свидетельства. " * 2
    criterion = "Отдельный принимающий подтверждает результат этой дорожки"
    for index in range(6):
        lead_admin.act(store, "W1", "issue", {
            "title": f"lane {index}", "body": body, "owns": [f"lanes/{index}/"],
            "resources": [f"device-{index}"], "criteria": [criterion],
        })
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE project='P' "
                         "AND tz LIKE 'sub:LEAD-%' AND state='ready'") == 6
    with pytest.raises(ValueError, match="ресурс пересекается"):
        lead_admin.act(store, "W1", "issue", {
            "title": "same device", "body": body, "owns": ["another/"],
            "resources": ["DEVICE-1"], "criteria": [criterion],
        })
    with pytest.raises(ValueError, match="зона пересекается"):
        lead_admin.act(store, "W1", "issue", {
            "title": "same files", "body": body, "owns": ["lanes/3/a.py"],
            "resources": ["another-device"], "criteria": [criterion],
        })


def test_owner_message_stays_in_brief_until_recorded_in_canon(env):
    store, root = env
    with store.tx() as tx:
        for mid, to in (("m-own1", "P/TZ-LEAD"), ("m-own2", "P/fix:TZ-LEAD:7")):
            tx.change("INSERT INTO messages(id,to_task,broadcast,from_kind,kind,body,created_at) "
                      "VALUES(?,?,0,'owner','note',?,?)", (mid, to, "Сделай XDrive", _now()),
                      event={"kind": "test_setup"})
    text = "\n".join(lead_admin.owner_lines(store, "P"))
    assert "m-own1" in text and "m-own2" in text and "НЕ ДОСТАВЛЕНО" in text
    (root / "ToDo/lead/LEDGER.json").write_text(json.dumps({"owner_reports": [{"message": "m-own1"}]}),
                                                encoding="utf-8")
    text = "\n".join(lead_admin.owner_lines(store, "P"))
    assert "m-own1" not in text and "m-own2" in text


def test_unread_and_changed_documents_are_listed(env):
    store, root = env
    (root / "README.md").write_text("readme", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "a.md").write_text("a", encoding="utf-8")
    sha = hashlib.sha256(b"old").hexdigest()
    (root / "ToDo/lead/LEDGER.json").write_text(json.dumps({"sources": {"README.md": {"sha256": sha}}}),
                                                encoding="utf-8")
    text = "\n".join(lead_admin.canon_lines(store, "P", {}))
    assert "новых 1, изменённых 1" in text and "README.md" in text and "docs/a.md" in text


def test_discovery_duty_until_recorded(env):
    store, _ = env
    assert lead_admin.discovery_lines(store, "P")
    with pytest.raises(ValueError, match="summary"):
        lead_admin.act(store, "W1", "discovery", {"summary": "мало"})
    lead_admin.act(store, "W1", "discovery", {"summary": "Прошёл путь установки и обновления: " * 2,
                                              "new": ["O-uninstall"]})
    assert lead_admin.discovery_lines(store, "P") == []


def test_project_config_overlay_whitelist(tmp_path, monkeypatch):
    root = tmp_path / "Proj"
    (root / "ops" / "lead").mkdir(parents=True)
    (root / "ops/lead/fleet.toml").write_text(
        'gate = "py -3 tools/check.py"\ncanon = ["docs/**/*.md"]\ncommit = false\n'
        '[live]\nevery_min = 30\n[[witness]]\nid = "w"\ncmd = ["py", "-3", "w.py"]\nevery_h = 2\n',
        encoding="utf-8")
    monkeypatch.setattr(project_config, "project_root", lambda p: str(root))
    merged = project_config.overlay("P", {"commit": True, "live": {"instance": "lib", "port": 8768}})
    assert merged["commit"] is True  # не из белого списка — у оператора
    assert merged["gate"] == "py -3 tools/check.py" and "docs/**/*.md" in merged["canon"]
    assert "README.md" in merged["canon"]  # канон по умолчанию не сужается
    assert merged["live"] == {"instance": "lib", "port": 8768, "every_min": 30.0}
    assert merged["witness"][0]["id"] == "w"
    assert any("commit" in e for e in merged["project_config"]["errors"])


def test_document_group_disposed_by_mask_with_reason(env):
    store, root = env
    (root / "docs" / "journal").mkdir(parents=True)
    (root / "docs" / "journal" / "d1.md").write_text("x", encoding="utf-8")
    (root / "ToDo/lead/LEDGER.json").write_text(json.dumps({"sources": {
        "docs/journal/*": {"reason": "журнал работ, обещаний человеку нет"}}}), encoding="utf-8")
    assert lead_admin.canon_lines(store, "P", {"canon": ["docs/**/*.md"]}) == []


def test_project_config_overlay_only_adds(tmp_path, monkeypatch):
    # 06.11 (Р1, фаза 0.1): fleet.toml ведущего не снимает и не подменяет свидетелей и гейт оператора.
    op = {"gate": "py -3 tools/gate.py", "witness": [{"id": "op", "cmd": ["py", "-3", "ops/witness/op.py"]}]}
    weak = tmp_path / "Weak"
    (weak / "ops" / "lead").mkdir(parents=True)
    (weak / "ops/lead/fleet.toml").write_text('gate = "echo ok"\nwitness = []\n', encoding="utf-8")
    monkeypatch.setattr(project_config, "project_root", lambda p: str(weak))
    merged = project_config.overlay("P", op)
    assert merged["gate"] == "py -3 tools/gate.py"
    assert [w["id"] for w in merged["witness"]] == ["op"]
    assert any(e.startswith("gate") for e in merged["project_config"]["errors"])
    add = tmp_path / "Add"
    (add / "ops" / "lead").mkdir(parents=True)
    (add / "ops/lead/fleet.toml").write_text(
        '[[witness]]\nid = "op"\ncmd = ["py", "-3", "fake.py"]\n'
        '[[witness]]\nid = "mine"\ncmd = ["py", "-3", "ops/witness/m.py"]\nlevel = "L2"\n', encoding="utf-8")
    monkeypatch.setattr(project_config, "project_root", lambda p: str(add))
    merged = project_config.overlay("P", op)
    assert [w["id"] for w in merged["witness"]] == ["op", "mine"]
    assert merged["witness"][0]["cmd"] == ["py", "-3", "ops/witness/op.py"]
    assert merged["witness"][1]["source"] == "ops/lead/fleet.toml"
    assert any("witness op" in e for e in merged["project_config"]["errors"])


def test_witness_receipt_carries_instrument_sha_and_pinned_change(tmp_path):
    # 06.11 (Р1, фаза 0.2): квитанция несёт sha прибора; закреплённый sha ловит правку прибора.
    from dev_mcp.fleet import witness
    script = tmp_path / "w.py"
    script.write_text('import json; print(json.dumps({"verdict": "works_live", "summary": "ok"}))\n',
                      encoding="utf-8")
    item = {"id": "w", "obligation": "O-x", "cmd": [sys.executable, "w.py"], "level": "L3"}
    first = witness.run_one(str(tmp_path), item)
    assert first["verdict"] == "works_live" and len(first["instrument_sha"]) == 64
    assert first["level"] == "L3" and first["cmd"][-1] == "w.py"
    assert witness.run_one(str(tmp_path), {**item, "sha": first["instrument_sha"][:12]})["verdict"] == "works_live"
    script.write_text(script.read_text(encoding="utf-8") + "# tweak\n", encoding="utf-8")
    changed = witness.run_one(str(tmp_path), {**item, "sha": first["instrument_sha"][:12]})
    assert changed["verdict"] == "instrument_changed" and changed["instrument_sha"] != first["instrument_sha"]


def test_instruction_objects_lifecycle_and_family_delivery(env):
    # 06.11 (фаза 0.3): письмо к закрытому раунду доставляется ведущему и становится поручением.
    from dev_mcp.fleet import inbox
    store, root = env
    with store.tx() as tx:
        tx.change("INSERT INTO messages(id,to_task,broadcast,from_kind,kind,body,created_at) "
                  "VALUES(?,?,0,'owner','note',?,?)", ("m-closed7", "P/fix:TZ-LEAD:7",
                                                       "Сделай XHTTP extra для VPNMIR", _now()),
                  event={"kind": "test_setup"})
    assert any(m.id == "m-closed7" for m in inbox.pending_for(store, "W1"))
    text = "\n".join(lead_admin.duty_lines(store, "P"))
    assert "i-closed7" in text and "без статуса 1" in text
    with pytest.raises(ValueError, match="нет в"):
        lead_admin.act(store, "W1", "instruction", {"id": "i-closed7", "status": "accepted",
                                                    "obligation": "O-xhttp"})
    (root / "ToDo/lead/LEDGER.json").write_text(json.dumps({"obligations": [{"id": "O-xhttp"}]}),
                                                encoding="utf-8")
    lead_admin.act(store, "W1", "instruction", {"id": "i-closed7", "status": "accepted", "obligation": "O-xhttp"})
    assert "без статуса 0" in "\n".join(lead_admin.duty_lines(store, "P"))
    with pytest.raises(ValueError, match="evidence"):
        lead_admin.act(store, "W1", "instruction", {"id": "i-closed7", "status": "done", "evidence": "x"})
    lead_admin.act(store, "W1", "instruction", {"id": "i-closed7", "status": "done",
                                                "evidence": "коммит abc123, свидетель works_live"})
    assert not any("i-closed7" in line for line in lead_admin.duty_lines(store, "P"))
    with pytest.raises(ValueError, match="закрыто"):
        lead_admin.act(store, "W1", "instruction", {"id": "i-closed7", "status": "declined",
                                                    "reason": "передумал совсем уже давно"})
    assert inbox.ack(store, "W1", ["m-closed7"]) == 1


def test_obstacles_park_charter_cards_and_owner_answer_unparks(env):
    # 06.11 (фаза 1.1): нечего продвинуть — устав паркуется, владелец получает карточку.
    from dev_mcp.fleet import instructions, lead, obstacles
    store, root = env
    (root / "ToDo/lead/LEDGER.json").write_text(json.dumps({"obligations": [
        {"id": "O-a", "status": "unknown"}, {"id": "O-b", "status": "owner_question"},
        {"id": "O-c", "status": "works_live"}]}), encoding="utf-8")
    assert obstacles.readiness(store, "P")[0] is True
    with pytest.raises(ValueError, match="option"):
        lead_admin.act(store, "W1", "obstacle", {"obligations": ["O-a"],
                                                 "ask": "Разрешаешь сбор данных с сайта X?"})
    # 07.11: вопрос владельцу — только после сверки с его разрешениями.
    with pytest.raises(ValueError, match="Разрешения|разрешениями владельца"):
        lead_admin.act(store, "W1", "obstacle", {"obligations": ["O-a"], "option": "да, публичное",
                                                 "ask": "Разрешаешь сбор данных с сайта X?"})
    why_not = "платный API сайта X: трата денег владельца, разрешения не покрывают"
    with pytest.raises(ValueError, match="нет в"):
        lead_admin.act(store, "W1", "obstacle", {"obligations": ["O-zzz"], "option": "да, публичное",
                                                 "ask": "Разрешаешь сбор данных с сайта X?",
                                                 "not_covered": why_not})
    out = lead_admin.act(store, "W1", "obstacle", {"obligations": ["O-a"], "option": "да, публичное",
                                                   "ask": "Разрешаешь сбор данных с сайта X?",
                                                   "not_covered": why_not})
    oid = out.split()[2]
    ok, why = obstacles.readiness(store, "P")
    assert ok is False and oid in why
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE project='P' AND tz='TZ-LEAD'", (),
                  event={"kind": "test_setup"})
        tx.record_event(kind="accept", project="P", tz="TZ-LEAD", payload={})
    assert lead.reopen(store) == []
    assert lead.reopen(store) == []
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='lead_parked'") == 1
    obstacles.write_cards(store)
    card = (Path(store.path).parent / "owner-cards" / "p.md").read_text(encoding="utf-8")
    assert f"owner-decision: fleet-p/{oid}" in card and "держит 1 O-*" in card
    assert any(oid in line for line in lead_admin.duty_lines(store, "P"))
    instructions.register(store, "P", text=f"По {oid}: да, только публичные страницы",
                          done_when="сбор идёт", author="owner")
    assert obstacles.resolve(store) == [f"P/{oid}"]
    assert lead.reopen(store) == ["P/TZ-LEAD"]
    with store.read() as tx:
        assert tx.one("SELECT 1 FROM events WHERE kind='lead_unparked'") is not None


def test_lead_progress_is_checkpoint_and_milestone_needs_evidence(env):
    # Progress is a checkpoint, not an accepted product milestone.
    from dev_mcp.fleet import lead
    store, root = env
    (root / "ToDo/lead/LEDGER.json").write_text(json.dumps({"obligations": [
        {"id": "O-a", "status": "unknown"}]}), encoding="utf-8")
    claim = {"project": "P", "tz": "TZ-LEAD"}
    with store.read() as tx:
        assert lead.submit_refusal(tx, claim, "починил парсер") == ""
        assert "забытое" in lead.submit_refusal(tx, claim, "веха: O-a L1→L2")
        assert "нет в книге" in lead.submit_refusal(tx, claim, "веха: O-zz L1→L2\nзабытое: нет")
        assert "L1" in lead.submit_refusal(tx, claim, "веха: O-a L1→L3\nзабытое: нет")
        assert "квитанции" in lead.submit_refusal(
            tx, claim, "веха: O-a L1→L2\nзабытое: письмо m-1 → поручение")
    (root / "ToDo/lead/LEDGER.json").write_text("{bad", encoding="utf-8")
    with store.read() as tx:
        assert lead.submit_refusal(tx, claim, "прогресс") == ""
        assert "JSON" in lead.submit_refusal(tx, claim, "веха: O-a L1→L2\nзабытое: нет")
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='submitted' WHERE project='P' AND tz='TZ-LEAD'", (),
                  event={"kind": "submit", "worker_id": "W1", "project": "P", "tz": "TZ-LEAD",
                         "payload": {"note": "починил парсер"}})
    assert lead.progress_accept(store, "P", "TZ-LEAD") == "server-progress:TZ-LEAD"
    with store.read() as tx:
        assert tx.one("SELECT state FROM tasks WHERE tz='TZ-LEAD'")["state"] == "ready"
        assert tx.one("SELECT 1 FROM events WHERE kind='lead_checkpoint'") is not None
        assert tx.one("SELECT 1 FROM events WHERE kind='accept'") is None
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='submitted' WHERE project='P' AND tz='TZ-LEAD'", (),
                  event={"kind": "submit", "worker_id": "W1", "project": "P", "tz": "TZ-LEAD",
                         "payload": {"note": "веха: O-a L1→L2\nзабытое: нет"}})
    assert lead.progress_accept(store, "P", "TZ-LEAD") is None
    text = "\n".join(lead.milestone_lines(store, "P"))
    assert "O-a L1→L2" in text and "Забытое (ведущий): нет" in text


def test_accepted_milestone_level_is_not_counted_twice(env):
    # 06–07.11: O-resource-benchmark «L1→L2» принят дважды; засчитанный уровень нигде не хранился.
    from dev_mcp.fleet import lead
    store, root = env
    (root / "ToDo/lead/LEDGER.json").write_text(json.dumps({"obligations": [
        {"id": "O-a", "status": "unknown"}, {"id": "O-b", "status": "unknown"}]}), encoding="utf-8")
    claim = {"project": "P", "tz": "TZ-LEAD"}
    with store.tx() as tx:
        tx.record_event(kind="lead_stage_evidence", project="P", payload={
            "obligation": "O-a", "stage": "L1", "sha": "abc", "receipt": "test:1",
            "test_passed": True})
        tx.record_event(kind="lead_stage_evidence", project="P", payload={
            "obligation": "O-a", "stage": "L2", "sha": "abc", "receipt": "main:abc",
            "review_accepted": True, "integrated": True})
        tx.record_event(kind="lead_milestone_claim", project="P", tz="TZ-LEAD",
                        payload={"obligation": "O-b", "from": "L1", "to": "L2"})
        tx.record_event(kind="reject", project="P", tz="TZ-LEAD", payload={"note": "нет"})
        tx.record_event(kind="lead_milestone_claim", project="P", tz="TZ-LEAD",
                        payload={"obligation": "O-a", "from": "L1", "to": "L2"})
        tx.record_event(kind="accept", project="P", tz="TZ-LEAD", payload={"progress": True})
        tx.record_event(kind="accept", project="P", tz="TZ-LEAD", payload={"note": "ok"})
    with store.read() as tx:
        assert set(lead.accepted_levels(tx, "P")) == {"O-a"}
    assert "O-a L2" in " ".join(lead.levels_lines(store, "P"))
    with store.read() as tx:
        assert "уже засчитан на L2" in lead.submit_refusal(tx, claim, "веха: O-a L1→L2\nзабытое: нет")
        assert "квитанции" in lead.submit_refusal(
            tx, claim, "веха: O-b L1→L2\nзабытое: нет")
        # «claim_id=… . веха: …» в одной строке — тоже веха, а не прогресс.
        assert "уже засчитан" in lead.submit_refusal(
            tx, claim, "claim_id=1. веха: O-a L1→L2\nзабытое: нет")


def test_owner_card_goes_to_telegram_once(env):
    # 07.11: владелец не узнавал о вопросах ведущих (TickTick не работал) — карточка в Telegram.
    from dev_mcp.fleet import owner_telegram
    store, root = env
    (root / "ToDo/lead/LEDGER.json").write_text(json.dumps({"obligations": [
        {"id": "O-a", "status": "unknown"}]}), encoding="utf-8")
    lead_admin.act(store, "W1", "obstacle", {
        "obligations": ["O-a"], "option": "да, оплачиваю", "ask": "Оплатить API сайта X на месяц?",
        "not_covered": "трата денег владельца — разрешения это не покрывают"})
    sent: list[str] = []
    none = lambda: []  # noqa: E731 — only obstacle cards in this test
    assert owner_telegram.tick(store, send=lambda t: sent.append(t) or True, decisions=none) == 1
    assert owner_telegram.tick(store, send=lambda t: sent.append(t) or True, decisions=none) == 0
    assert "Оплатить API" in sent[0] and "трата денег" in sent[0] and "fleet.cmd unblock" in sent[0]


def test_owner_answer_thaws_frozen_lead_charter(env):
    # 07.11: local-library заморозил устав block без пробы; ответ владельца его не вернул.
    from dev_mcp.fleet import obstacles
    store, root = env
    (root / "ToDo/lead/LEDGER.json").write_text(json.dumps({"obligations": [
        {"id": "O-a", "status": "unknown"}]}), encoding="utf-8")
    out = lead_admin.act(store, "W1", "obstacle", {
        "obligations": ["O-a"], "option": "да, оплачиваю", "ask": "Оплатить API сайта X на месяц?",
        "not_covered": "трата денег владельца — разрешения это не покрывают"})
    oid = out.split()[2]
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='frozen' WHERE project='P' AND tz='TZ-LEAD'", (),
                  event={"kind": "block", "project": "P", "tz": "TZ-LEAD", "payload": {"reason": oid}})
    assert obstacles.close(store, "P", oid, by="owner", answer="да")
    with store.read() as tx:
        assert tx.one("SELECT state FROM tasks WHERE tz='TZ-LEAD'")["state"] == "ready"


def test_blocked_charter_wakes_for_open_instruction_or_unacked_letter(env):
    from dev_mcp.fleet import instructions, lead, obstacles
    store, root = env
    (root / "ToDo/lead/LEDGER.json").write_text(json.dumps({"obligations": [
        {"id": "O-a", "status": "owner_question"}]}), encoding="utf-8")
    assert obstacles.readiness(store, "P")[0] is False
    instructions.register(store, "P", iid="i-owner", text="Fix current VPN CPU overload",
                          done_when="CPU load checked", author="owner")
    assert obstacles.readiness(store, "P")[0] is True
    with store.tx() as tx:
        tx.record_event(kind="instruction_status", project="P", tz="TZ-LEAD",
                        payload={"id": "i-owner", "status": "done"})
    assert obstacles.readiness(store, "P")[0] is False
    with store.tx() as tx:
        tx.change("INSERT INTO messages(id,to_task,broadcast,from_kind,kind,body,created_at) "
                  "VALUES(?,?,0,'owner','note',?,?)",
                  ("m-owner", "P/fix:TZ-LEAD:7", "Fresh owner letter", _now()),
                  event={"kind": "test_setup"})
    ok, why = obstacles.readiness(store, "P")
    assert ok is True and "m-owner" in why
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE project='P' AND tz='TZ-LEAD'", (),
                  event={"kind": "test_setup"})
        tx.record_event(kind="accept", project="P", tz="TZ-LEAD", payload={})
    assert lead.reopen(store) == ["P/TZ-LEAD"]
    with store.read() as tx:
        assert tx.one("SELECT state FROM tasks WHERE project='P' AND tz='TZ-LEAD'")["state"] == "ready"
        assert tx.one("SELECT 1 FROM tasks WHERE project='P' AND tz LIKE 'fix:TZ-LEAD%' "
                      "AND state='ready'") is None
    with store.tx() as tx:
        tx.change("UPDATE messages SET acked_at=? WHERE id='m-owner'", (_now(),),
                  event={"kind": "test_setup"})
    assert obstacles.readiness(store, "P")[0] is False


def test_readiness_takes_obligation_state_from_evidence(env, monkeypatch):
    # 08.11 (LEAD-SYSTEM-DESIGN 2.1): книга пишет works_live, а свидетель красный — O-* снова
    # продвигаемо; принятый прибор подтвердил уровень — завершено, хоть книга и отстала.
    from dev_mcp.fleet import obstacles, product
    store, root = env
    (root / "ToDo/lead/LEDGER.json").write_text(json.dumps({"obligations": [
        {"id": "O-a", "status": "works_live"}, {"id": "O-b", "status": "unknown"},
        {"id": "O-c", "status": "works_live"}]}), encoding="utf-8")
    states = {"O-a": {"id": "O-a", "met": False, "broken": False},
              "O-b": {"id": "O-b", "met": True, "broken": False}}
    monkeypatch.setattr(product, "state", lambda *_a, **_k: list(states.values()))
    ok, why = obstacles.readiness(store, "P")
    assert ok is False and "завершены" in why
    states["O-c"] = {"id": "O-c", "met": False, "broken": True}
    ok, why = obstacles.readiness(store, "P")
    assert ok is True and "1" in why
