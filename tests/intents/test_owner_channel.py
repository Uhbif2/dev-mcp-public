"""TZ-303: durable owner notification via test-only, offline adapters."""
from __future__ import annotations

import json
from pathlib import Path

from dev_mcp.fleet.store import Store
from dev_mcp.intents import owner_channel, stages


def _store(tmp_path):
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)",
                  ("Demo", str(tmp_path)), event={"kind": "test.project"})
    return store


class _Toast:
    def __init__(self):
        self.calls = []

    def show(self, url, *, notice_kind='done'):
        self.calls.append((url,notice_kind))
        return True


class _TickTick:
    def __init__(self):
        self.created = []
        self.done = False

    def create(self, token, title, content, key):
        self.created.append((token, title, content, key))
        return {"id": "task-19", "projectId": "list-1"}

    def completed(self, token, project_id, task_id):
        assert project_id == "list-1" and task_id == "task-19"
        return self.done


def test_server_channel_writes_inbox_once_per_text(monkeypatch, tmp_path):
    store = _store(tmp_path)
    inbox, toast = tmp_path / "owner-inbox.md", _Toast()
    monkeypatch.setattr(owner_channel, "INBOX_PATH", inbox)
    monkeypatch.setattr(owner_channel, "_toast", lambda: toast)
    first = owner_channel.notify_owner(store, "Demo", "needs",
                                       "Одно физическое действие от владельца")
    second = owner_channel.notify_owner(store, "Demo", "needs",
                                        "Одно физическое действие от владельца")
    assert first == second
    assert inbox.read_text(encoding="utf-8").count("Одно физическое действие") == 1
    assert len(toast.calls) == 1
    with store.read() as tx:
        sent = tx.query("SELECT payload FROM events WHERE kind='owner_channel.sent'")
        fallback = tx.query("SELECT payload FROM events WHERE kind='owner_channel.fallback'")
    assert len(sent) == 1 and len(fallback) == 0  # TickTick only via OAuth connector, no token branch


def test_failed_toast_remains_pending_and_recovers_once_after_restart(monkeypatch,tmp_path):
    store=_store(tmp_path)
    monkeypatch.setattr(owner_channel,'INBOX_PATH',tmp_path/'inbox.md')
    class Toast:
        accepted=False
        calls=0
        def show(self,url,*,notice_kind='done'):
            assert notice_kind=='needs'
            self.calls+=1
            return self.accepted
    toast=Toast()
    monkeypatch.setattr(owner_channel,'_toast',lambda:toast)
    result=owner_channel.notify_owner(store,'Demo','needs','Review the versioned plan')
    assert result['delivery_state']=='pending' and result['inbox_written'] is True
    assert owner_channel.retry_pending_toasts(store)==0
    monkeypatch.setattr(owner_channel,'_now',lambda:'2099-01-01T00:00:00+00:00')
    toast.accepted=True
    store.close()
    reopened=Store(str(tmp_path/'fleet.sqlite'),available=True)
    assert owner_channel.retry_pending_toasts(reopened)==1
    assert owner_channel.retry_pending_toasts(reopened)==0
    assert toast.calls==2
    assert (tmp_path/'inbox.md').read_text(encoding='utf-8').count('Review the versioned plan')==1
    reopened.close()


def test_ops_approval_notice_waits_for_versioned_independent_plan_proof(monkeypatch,tmp_path):
    from dev_mcp.fleet import outcome
    store=_store(tmp_path)
    outcome.install_schema(store)
    outcome.create_outcome(store,project='Demo',title='root',intent_id='i',spec={'goal':'test'},
                           evidence_spec={'kind':'run'},authority={'source':'owner'},outcome_id='root')
    outcome.create_outcome(store,project='Demo',title='rollback_plan',intent_id='i',spec={'goal':'test'},
                           evidence_spec={'kind':'review','subject_version':'v1'},authority={'source':'owner'},
                           outcome_id='plan',parent_id='root')
    outcome.create_outcome(store,project='Demo',title='owner_approved',intent_id='i',
                           spec={'depends_on':['plan']},evidence_spec={'kind':'review'},authority={'source':'owner'},
                           outcome_id='approval',parent_id='root')
    (tmp_path/'intent').mkdir()
    (tmp_path/'intent/rollback-plan.md').write_text('## Нужно от владельца\nApprove the bounded pilot', encoding='utf-8')
    toast=_Toast()
    monkeypatch.setattr(owner_channel,'INBOX_PATH',tmp_path/'inbox.md')
    monkeypatch.setattr(owner_channel,'_toast',lambda:toast)
    assert owner_channel.notify_ready_ops_plans(store)==0
    outcome.add_evidence(store,writer=outcome.server_evidence_writer(store),outcome_id='plan',
                         kind='review',ref='independent-plan',subject_version='v1',
                         env_version='test',verdict='pass',producer='test-server')
    outcome.close_outcome(store,'plan')
    assert owner_channel.notify_ready_ops_plans(store)==1
    owner_channel.notify_ready_ops_plans(store)
    assert len(toast.calls)==1 and toast.calls[0][1]=='needs'
    with store.read() as tx:
        assert tx.scalar("SELECT state FROM outcomes WHERE id='approval'")=='open'
    assert 'SHA-256' in (tmp_path/'inbox.md').read_text(encoding='utf-8')
    store.close()


def test_section_extraction_suppresses_nothing_and_keeps_done(tmp_path):
    understood = tmp_path / "understood.md"
    understood.write_text("## Нужно от владельца\nничего\n\n## Другое\nДа\n",
                          encoding="utf-8")
    handover = tmp_path / "handover.md"
    handover.write_text("## Готово\nСервис проверен и установлен\n\n"
                        "## От владельца\nничего\n", encoding="utf-8")
    assert stages._owner_stage_notice("understood", understood) is None
    assert stages._owner_stage_notice("handed_over", handover) == (
        "done", "Сервис проверен и установлен"
    )


def test_prover_sends_owner_needs_only_after_successful_proof(monkeypatch, tmp_path):
    from dev_mcp.fleet import controller as ctl
    inbox, parent = tmp_path / "intents", tmp_path / "hub"
    inbox.mkdir(); parent.mkdir()
    monkeypatch.setattr(ctl, "INTENT_INBOX", inbox)
    monkeypatch.setattr(ctl, "INTENT_PARENT", parent)
    monkeypatch.setattr(ctl, "INTENT_ROOTS", (parent,))
    (inbox / "demo.md").write_text("сделай тест", encoding="utf-8")
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    ctl._poll_owner_intents(store)
    ctl.Controller(store, intent_project=ctl.owner_intent_project,
                   maintenance=lambda: None).tick()
    notices = []
    monkeypatch.setattr(stages, "_trusted_owner_store", lambda _: True)
    monkeypatch.setattr(owner_channel, "notify_owner",
                        lambda store, project, kind, text:
                        notices.append((project, kind, text)))
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE project='demo' "
                  "AND tz LIKE '%-understood'", event="test.accept")
    assert stages.prove_once(store) == 0
    assert notices == []
    project = parent / "demo"
    sections = ["Интенция", "Готовые решения", "Что уже есть", "Ограничения",
                "Неясности", "Нужно от владельца"]
    document = "kind: build\ntarget: new\n\n" + "\n".join("## " + s + "\n" +
                         ("Нужна физическая проверка владельца" if s == sections[-1]
                          else "Реальное содержимое " * 12)
                         for s in sections)
    (project / "intent").mkdir(exist_ok=True)
    (project / "intent" / "understood.md").write_text(document, encoding="utf-8")
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE project='demo' "
                  "AND tz LIKE '%-understood'", event="test.accept")
    assert stages.prove_once(store) == 1
    assert notices == [("demo", "needs", "Нужна физическая проверка владельца")]
