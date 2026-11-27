"""28.10.2026: ведущий проекта — одна долгая задача TZ-LEAD*."""
from __future__ import annotations

import json
from pathlib import Path

from dev_mcp.fleet import lead, wake
from dev_mcp.fleet.store import Store


def _store(tmp_path: Path) -> Store:
    return Store.open(str(tmp_path / "fleet.sqlite"))


def _task(store: Store, tz: str, state: str) -> None:
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES('P','C:/p',0,1) "
                  "ON CONFLICT DO NOTHING", (), event="test_setup")
        tx.change("INSERT INTO tasks(project,tz,title,state,priority,kind,owns,body,created_at,updated_at) "
                  "VALUES('P',?,?,?,0,'work','[]','',  '2026-10-28','2026-10-28')", (tz, tz, state), event="test_setup")


def _event(store: Store, tz: str, kind: str, payload: dict) -> None:
    with store.tx() as tx:
        tx.record_event(kind=kind, project="P", tz=tz, payload=payload)


def test_wake_text_asks_lead_to_continue_delivery(monkeypatch):
    from dev_mcp.fleet import goals

    monkeypatch.setattr(goals, "project_info", lambda project: {
        "goal": "Читатель открывает отчёт.",
        "lead_focus": {"action": "Проверь открытие.", "evidence": "Отчёт виден читателю."},
    })
    text = wake.wake_text({"project": "P", "tz": "fix:TZ-LEAD:7", "worker_id": "W1", "wake": "w"})
    assert "Проверь открытие." in text and "Отчёт виден читателю." in text
    assert "fleet_resume" in text and "проверку результата в месте использования" in text
    assert "fleet_quick" in text and "fleet_lead(action='issue')" in text
    assert "Сдачу кода считай этапом" not in text
    assert "только когда цель достигнута" not in text and "бери следующую" not in text
    normal = wake.wake_text({"project": "P", "tz": "TZ-7", "worker_id": "W1", "wake": "w"})
    assert "fleet_submit" in normal


def test_integrated_lead_milestone_reopens(tmp_path):
    store = _store(tmp_path)
    _task(store, "TZ-LEAD", "accepted")
    _task(store, "TZ-8", "accepted")
    _event(store, "TZ-LEAD", "accept", {})
    _event(store, "TZ-8", "accept", {})
    # 05.11: устав возвращается сразу после приёмки — коммит и гейт ведущего не держат.
    assert lead.reopen(store) == ["P/TZ-LEAD"]
    with store.read() as tx:
        states = dict((r["tz"], r["state"]) for r in tx.query("SELECT tz,state FROM tasks"))
    assert states == {"TZ-LEAD": "ready", "TZ-8": "accepted"}


def test_fix_of_lead_is_lead_but_review_is_not():
    assert lead.is_lead("fix:TZ-LEAD") and lead.is_lead("fix:TZ-LEAD:2") and lead.is_lead("TZ-LEAD")
    assert not lead.is_lead("review:TZ-LEAD") and not lead.is_lead("TZ-7")


def test_window_lines_one_focus_and_bounded_brief(tmp_path):
    store = _store(tmp_path)
    for tz in ("TZ-LEAD", "fix:TZ-LEAD", "fix:TZ-LEAD", "TZ-8"):
        _event(store, tz, "take", {})
    text = "\n".join(lead.window_lines(store, "P"))
    assert "последняя проверенная стадия" in text and "STATE.md" in text
    assert "Блокер:" in text and "владелец:" in text
    assert "Одно действие:" in text and len(text) <= 2500


def test_progress_distinguishes_milestones_commits_and_live_evidence(tmp_path):
    store = _store(tmp_path)
    _event(store, 'witness:x', 'witness_result', {'id': 'x', 'verdict': 'unknown'})
    _event(store, 'TZ-LEAD', 'accept', {})
    with store.read() as tx:
        aid = tx.scalar("SELECT MAX(id) FROM events WHERE kind='accept'")
    _event(store, 'TZ-LEAD', 'integrate', {'accept_id': aid, 'reason': 'launch_error'})
    _event(store, 'TZ-LEAD', 'integrate', {'accept_id': aid, 'deferred': ['x']})
    _event(store, 'OTHER', 'accept', {})
    text = '\n'.join(lead.progress_lines(store, 'P'))
    assert 'принято вех 1' in text and 'есть коммит у 0' in text
    assert 'новых переходов в works_live 0' in text and 'OWNER.md' in text and '<!-- owner-decision:' in text
    _event(store, 'TZ-LEAD', 'integrate', {'accept_id': aid, 'commit': 'abc'})
    # Retries of the same successful commit do not inflate the result.
    _event(store, 'TZ-LEAD', 'integrate', {'accept_id': aid, 'commit': 'abc'})
    _event(store, 'witness:x', 'witness_result', {'id': 'x', 'verdict': 'works_live'})
    _event(store, 'witness:x', 'witness_result', {'id': 'x', 'verdict': 'works_live'})
    text = '\n'.join(lead.progress_lines(store, 'P'))
    assert 'есть коммит у 1' in text and 'новых переходов в works_live 1' in text
    assert 'не продвинулось' not in text


def test_progress_does_not_call_repeated_green_a_new_live_result(tmp_path):
    store = _store(tmp_path)
    with store.tx() as tx:
        tx.record_event(kind='witness_result', project='P', tz='witness:x',
                        payload={'id': 'x', 'verdict': 'works_live'})
        tx.change("UPDATE events SET ts='2020-01-01T00:00:00+00:00' WHERE kind='witness_result'", (),
                  event='test_setup')
    _event(store, 'TZ-LEAD', 'accept', {})
    _event(store, 'witness:x', 'witness_result', {'id': 'x', 'verdict': 'works_live'})
    assert 'новых переходов в works_live 0' in '\n'.join(lead.progress_lines(store, 'P'))
    # A six-hour witness need not rerun within the three-hour feedback window.
    with store.tx() as tx:
        tx.change("UPDATE events SET ts='2020-01-01T00:00:00+00:00' WHERE kind='witness_result'", (),
                  event='test_setup')
    assert 'не продвинулось' in '\n'.join(lead.progress_lines(store, 'P'))


def test_accepted_fix_of_lead_also_reopens(tmp_path):
    # Отклонённая веха превращается в fix:TZ-LEAD; принятый fix тоже должен вернуть ведущего.
    store = _store(tmp_path)
    _task(store, "fix:TZ-LEAD", "accepted")
    _event(store, "fix:TZ-LEAD", "accept", {})
    _event(store, "fix:TZ-LEAD", "integrate", {"commit": "abc"})
    assert lead.reopen(store) == ["P/fix:TZ-LEAD"]


def test_owns_drift_is_reported_to_lead(tmp_path):
    from dev_mcp.fleet.model import norm_path
    root = tmp_path / "proj"
    (root / "ToDo" / "tz").mkdir(parents=True)
    spec = root / "ToDo" / "tz" / "TZ-LEAD-x.md"
    spec.write_text("```yaml\ntz: TZ-LEAD\nowns: [ops/lead]\nneeds: []\n"
                    "acceptance: [py -3 -c pass]\n```\n", encoding="utf-8")
    store = _store(tmp_path)
    wide = [norm_path(str(root / "client")), norm_path(str(root / "ops/lead"))]
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES('Q',?,0,1)", (str(root),),
                  event="test_setup")
        for tz in ("TZ-LEAD", "fix:TZ-LEAD"):
            tx.change("INSERT INTO tasks(project,tz,title,state,priority,kind,owns,spec_path,body,"
                      "created_at,updated_at) VALUES('Q',?,?,'ready',0,'work',?,?,'','t','t')",
                      (tz, tz, json.dumps(wide), "ToDo/tz/TZ-LEAD-x.md" if tz == "TZ-LEAD" else ""),
                      event="test_setup")
    text = "\n".join(lead.owns_drift_lines(store, "Q", "fix:TZ-LEAD"))
    assert "client" in text and "Действует реестр" in text
    spec.write_text(spec.read_text(encoding="utf-8").replace("[ops/lead]", "[client, ops/lead]"),
                    encoding="utf-8")
    assert lead.owns_drift_lines(store, "Q", "fix:TZ-LEAD") == []


def test_lead_milestone_review_gets_ledger_sample(tmp_path):
    root = tmp_path / "proj"
    (root / "ToDo" / "lead").mkdir(parents=True)
    sections = {f"Раздел {i}": ["O-generic"] for i in range(10)}
    sections["Особый"] = ["O-dns"]
    (root / "ToDo" / "lead" / "LEDGER.json").write_text(json.dumps(
        {"sources": {"product/A.md": {"sections": sections}}}, ensure_ascii=False), encoding="utf-8")
    store = _store(tmp_path)
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES('Q',?,0,1)", (str(root),),
                  event="test_setup")
    text = "\n".join(lead.review_lines(store, "Q", "review:fix:TZ-LEAD:2"))
    assert "product/A.md" in text and "O-generic привязано к 10 из 11" in text
    assert "не повод отклонять" in text and "выбранной владельцем" in text
    assert "без свидетеля" in text and "отрицательного контроля" in text
    # 08.11: холодный шаг — первым, до книги и до рассказа ведущего.
    assert text.startswith("Холодное ревью") and text.index("холодно:") < text.index("product/A.md")
    assert lead.review_lines(store, "Q", "review:TZ-7") == []
    assert lead.review_lines(store, "Q", "fix:TZ-LEAD") == []


def test_lead_reopens_when_project_does_not_commit(tmp_path):
    # 03.11 Nova VPN: integrate{skipped: «коммит не разрешён…»} — ведущий всё равно продолжает.
    store = _store(tmp_path)
    _task(store, "fix:TZ-LEAD:2", "accepted")
    _event(store, "fix:TZ-LEAD:2", "accept", {})
    _event(store, "fix:TZ-LEAD:2", "integrate",
           {"skipped": "коммит не разрешён проектом (fleet-projects.toml: commit = true)"})
    assert lead.reopen(store) == ["P/fix:TZ-LEAD:2"]


def test_accepted_fix_returns_charter_once_not_itself(tmp_path):
    # 03.11–04.11 Nova VPN: fix:TZ-LEAD:3 возвращался сам 266 раз — «уже исправлено» → accept → снова.
    store = _store(tmp_path)
    _task(store, "TZ-LEAD", "accepted")
    _task(store, "fix:TZ-LEAD:3", "accepted")
    _event(store, "fix:TZ-LEAD:3", "accept", {})
    _event(store, "fix:TZ-LEAD:3", "integrate",
           {"skipped": "коммит не разрешён проектом (fleet-projects.toml: commit = true)"})
    assert lead.reopen(store) == ["P/TZ-LEAD"]
    with store.read() as tx:
        states = dict((r["tz"], r["state"]) for r in tx.query("SELECT tz,state FROM tasks"))
    assert states == {"TZ-LEAD": "ready", "fix:TZ-LEAD:3": "accepted"}
    # Устав снова принят, но своей интеграции ещё нет: старая приёмка fix его не возвращает.
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE tz='TZ-LEAD'", (), event="test_setup")
    _event(store, "TZ-LEAD", "accept", {})
    assert lead.reopen(store) == ["P/TZ-LEAD"]   # своя приёмка устава, не старая приёмка fix


def test_accepted_fix_does_not_duplicate_active_lead(tmp_path):
    store = _store(tmp_path)
    _task(store, "TZ-LEAD", "accepted")
    _task(store, "fix:TZ-LEAD:2", "accepted")
    _task(store, "fix:TZ-LEAD:3", "taken")
    _event(store, "fix:TZ-LEAD:2", "accept", {})
    _event(store, "fix:TZ-LEAD:2", "integrate", {"commit": "abc"})
    assert lead.reopen(store) == []
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE tz='fix:TZ-LEAD:3'", (), event="test_setup")
    assert lead.reopen(store) == []           # приёмка fix:2 уже учтена, повторно не всплывает


def test_lead_continues_when_milestone_commit_waits_for_foreign_work(tmp_path):
    # 04.11 19:38 – 05.11 05:00 Nova VPN: fix:TZ-LEAD:5 принят, коммит 11 раз отложен «unfinished
    # foreign writes» (TZ-170, TZ-262, request:dd5faeaf) — устав не возвращался, ведущий стоял ночь.
    store = _store(tmp_path)
    _task(store, "TZ-LEAD", "accepted")
    _task(store, "fix:TZ-LEAD:5", "accepted")
    _event(store, "fix:TZ-LEAD:5", "accept", {})
    _event(store, "fix:TZ-LEAD:5", "integrate",
           {"deferred": ["a.py"], "waiting_for": ["TZ-170"],
            "reason": "unfinished foreign writes inside candidate paths"})
    assert lead.reopen(store) == ["P/TZ-LEAD"]


def test_lead_does_not_wait_for_revalidation_of_its_own_bytes(tmp_path):
    store = _store(tmp_path)
    _task(store, "TZ-LEAD", "accepted")
    _task(store, "fix:TZ-LEAD:6", "accepted")
    _event(store, "fix:TZ-LEAD:6", "accept", {})
    _event(store, "fix:TZ-LEAD:6", "integrate",
           {"deferred": ["a.py"], "waiting_for": ["revalidate:x"],
            "reason": "accepted candidate bytes are not sealed"})
    assert lead.reopen(store) == ["P/TZ-LEAD"]


def test_lead_memo_shows_last_review_verdicts(tmp_path):
    # 05.11: третий чат собрался сдать веху, дважды отклонённую ревью, — вердиктов он не видел.
    store = _store(tmp_path)
    _task(store, "TZ-LEAD", "submitted")
    _event(store, "TZ-LEAD", "reject", {"note": "claim_id=1. REJECT: книга содержит шаблонные связи"})
    _event(store, "fix:TZ-LEAD:5", "accept", {"note": "ledger_check rc=0"})
    lines = lead.verdict_lines(store, "P")
    assert "отклонённое повторно не сдавай" in lines[0]
    assert lines[1].startswith("  ✓ принята fix:TZ-LEAD:5")
    assert "✗ отклонена TZ-LEAD" in lines[2] and "шаблонные связи" in lines[2]
    assert lead.verdict_lines(store, "Q") == []


def test_lead_milestone_without_new_writes_after_verdict_is_refused(tmp_path):
    # 05.11 Nova VPN: та же Android-веха сдана четырежды; последний чат лишь прогнал acceptance.
    store = _store(tmp_path)
    _task(store, "TZ-LEAD", "taken")
    claim = {"project": "P", "tz": "TZ-LEAD", "worker_id": "W1"}
    with store.read() as tx:
        assert lead.resubmit_reason(tx, claim) == ""           # вердиктов ещё нет
    _event(store, "TZ-LEAD", "accept", {})
    with store.tx() as tx:
        tx.record_event(kind="write", project="P", tz="TZ-LEAD", path_norm="c:/p/todo/lead/state.md")
        tx.record_event(kind="write", project="P", tz="TZ-LEAD", path_norm="c:/p/todo/lead/log/report.md")
        tx.record_event(kind="live_deployed", project="P", worker_id="W2", payload={"ok": True, "rev": "other"})
    with store.read() as tx:
        assert "Записи в книге и журнале не требуют сдачи" in lead.resubmit_reason(tx, claim)
        assert lead.resubmit_reason(tx, {"project": "P", "tz": "TZ-9"}) == ""
    with store.tx() as tx:
        tx.record_event(kind="live_deployed", project="P", worker_id="W1", payload={"ok": True, "rev": "release"})
    with store.read() as tx:
        assert lead.resubmit_reason(tx, claim) == ""
    _event(store, "TZ-LEAD", "accept", {})
    with store.tx() as tx:
        tx.record_event(kind="live_deployed", project="P", worker_id="W1", payload={"ok": True, "rev": "release"})
    with store.read() as tx:
        assert lead.resubmit_reason(tx, claim)
    with store.tx() as tx:
        tx.record_event(kind="write", project="P", tz="TZ-LEAD", path_norm="c:/p/deploy/field_receiver.py")
    with store.read() as tx:
        assert lead.resubmit_reason(tx, claim) == ""
