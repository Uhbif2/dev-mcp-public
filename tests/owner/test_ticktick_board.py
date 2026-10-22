"""Board sync against an in-memory TickTick (no network, no OAuth)."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dev_mcp.fleet.store import Store
from dev_mcp.owner import ticktick_board as tb
from dev_mcp.owner import ticktick_oauth as oauth


@pytest.fixture(autouse=True)
def isolated_stack_titles(monkeypatch):
    # Presentation tests provide their own component labels, independent of the host.
    monkeypatch.setattr(tb, '_stack_titles', lambda: {'second-pc': 'Second PC'})


class FakeTickTick:
    def __init__(self):
        self.projects, self.columns, self.tasks, self.comments, self.tags = [], [], {}, [], []
        self.calls = []
        self.n = 0

    def _id(self):
        self.n += 1
        return f"{self.n:024x}"

    def call(self, name, args):
        self.calls.append(name)
        if name == "list_projects":
            return self.projects
        if name == "create_project":
            p = {"id": self._id(), "name": args["name"]}
            self.projects.append(p)
            return p
        if name == "list_columns":
            return [c for c in self.columns if c["projectId"] == args["project_id"]]
        if name == "create_column":
            c = {"id": self._id(), "projectId": args["project_id"], **args["column"]}
            self.columns.append(c)
            return c
        if name == "list_tags":
            return [{"name": t} for t in self.tags]
        if name == "create_tag":
            self.tags.append(args["tag"]["name"])
            return args["tag"]
        if name == "create_task":
            t = dict(args["task"], id=self._id())
            t.setdefault("tags", [])
            t["items"] = [dict(i, id=self._id()) for i in t.get("items") or []]
            self.tasks[t["id"]] = t
            return t
        if name == "update_task":
            if args["task_id"] not in self.tasks:
                raise tb.BoardUnavailable("gone")
            self.tasks[args["task_id"]].update({k: v for k, v in args["task"].items() if k != "id"})
            return self.tasks[args["task_id"]]
        if name == "get_task_by_id":
            return self.tasks.get(args["task_id"])
        if name == "filter_tasks":
            wanted = set(args["filter"].get("projectIds") or [])
            tags = set(args["filter"].get("tag") or [])
            return [t for t in self.tasks.values() if t.get("status", 0) == 0
                    and (not wanted or t.get("projectId") in wanted)
                    and (not tags or tags & set(t.get("tags") or []))]
        if name == "get_comment":
            return [c for c in self.comments if c["task"] == args["task_id"]]
        if name == "move_task":
            for move in args["moves"]:
                self.tasks[move["taskId"]]["projectId"] = move["toProjectId"]
            return {}
        if name == "update_column":
            col = next(c for c in self.columns if c["id"] == args["column_id"])
            col.update({k: v for k, v in args["column"].items() if v is not None})
            return col
        if name == "delete_task":
            self.tasks.pop(args["task_id"], None)
            return {}
        if name == "add_comment":
            c = {"id": self._id(), "task": args["task_id"], "title": args["title"]}
            self.comments.append(c)
            return c
        raise AssertionError(name)


def make_store(tmp_path, root):
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,active) VALUES('second-pc-intent-x',?,1)",
                  (str(root),), event="seed")
        tx.change("INSERT INTO projects(name,root,active) VALUES('Beta App',?,1)",
                  (str(root),), event="seed")
        tx.change("INSERT INTO tasks(project,tz,title,state,created_at,updated_at) "
                  "VALUES('Beta App','request:1','Request: TZ-506 — иконка вопроса','taken','t','t')",
                  event="seed")
        now = datetime.now(timezone.utc).isoformat()
        tx.change("INSERT INTO workers(id,label,kind,first_seen,last_seen) VALUES('W-live','w','chatgpt',?,?)",
                  (now, now), event="seed")
        tx.change("INSERT INTO claims(project,tz,worker_id,state,started_at) "
                  "VALUES('Beta App','request:1','W-live','active',?)", (now,), event="seed")
    return store


SNAP = {"status": "ready", "human": {
    "projects": [{"project": "second-pc-intent-x", "title": "Локальный ИИ на втором ПК",
                  "stages": [{"label": "a", "done": True}, {"label": "b", "done": False}],
                  "current": "Решение"}],
    "actions": [{"project": "second-pc-intent-x", "title": "Локальный ИИ", "action_id": "a" * 20,
                 "action": "Одобрить операционный план с откатом", "default": "Оставить без подтверждения",
                 "reason": "Для этого шага необходимо действие владельца"}],
    "fleet": {"agents": 2, "queue": 5}}}


def board(tmp_path):
    root = tmp_path / "proj"
    (root / "intent").mkdir(parents=True)
    (root / "intent" / "rollback-plan.md").write_text("план", encoding="utf-8")
    store = make_store(tmp_path, root)
    fake = FakeTickTick()
    clock = lambda: datetime(2026, 10, 19, 17, 0, tzinfo=timezone.utc)
    return store, fake, tb.Board(store, fake, state_path=tmp_path / "board.json", clock=clock), root


def test_sync_builds_sections_notes_card_and_terse_question(tmp_path):
    store, fake, b, _ = board(tmp_path)
    b.sync(SNAP, system_lines=["агентов: 2"])
    names = [c["name"] for c in fake.columns]
    assert "◎ Система" in names and "＋ Новое" in names and "Beta App" in names
    assert "Second PC" in names  # an intent project lands in its target component's section
    assert any(t.get("title", "").startswith("Локальный ИИ") for t in fake.tasks.values())
    notes = [t for t in fake.tasks.values() if t["kind"] == "NOTE"]
    assert all(not t.get("parentId") for t in notes)  # TickTick: a NOTE cannot be a subtask
    bs = next(t for t in notes if "Агент работает (1)" in t["content"])
    assert "иконка вопроса" in bs["content"] and "TZ-506" not in bs["content"]
    question = next(t for t in fake.tasks.values() if t["kind"] == "CHECKLIST")
    assert question["title"].startswith("◆ Одобрить")
    assert "необходимо действие владельца" not in question["desc"]  # boilerplate never reaches the owner
    assert [i["title"] for i in question["items"]] == ["Да, выполняйте", "Нет"]
    card = fake.tasks[question["parentId"]]
    assert "нужно-решение" in card["tags"]


def test_second_sync_writes_nothing_when_nothing_changed(tmp_path):
    store, fake, b, _ = board(tmp_path)
    b.sync(SNAP, system_lines=["x"])
    b.sync(SNAP, system_lines=["x"])  # the brief request queued on the first cycle shows up once
    fake.calls.clear()
    b.sync(SNAP, system_lines=["x"])
    assert not [c for c in fake.calls if c.startswith(("create", "update", "add"))]


def test_tick_cannot_approve_a_plan_changed_after_the_question(tmp_path):
    store, fake, b, root = board(tmp_path)
    b.sync(SNAP, system_lines=["x"])
    question = next(t for t in fake.tasks.values() if t["kind"] == "CHECKLIST")
    (root / "intent/rollback-plan.md").write_text("new plan", encoding="utf-8")
    question["items"][0]["status"] = 1
    b.sync(SNAP, system_lines=["x"])
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='intent.ops_approved'") == 0
    assert question["items"][0]["status"] == 0
    question["items"][0]["status"] = 1
    b.sync(SNAP, system_lines=["x"])
    with store.read() as tx:
        records = tx.query("SELECT payload FROM events WHERE kind='owner.ticktick_ops_requested'")
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='intent.ops_approved'") == 0
    assert len(records) == 1
    assert json.loads(records[0]["payload"])["plan_sha256"] == hashlib.sha256(b"new plan").hexdigest()


def test_question_uses_explicit_zoned_deadline_and_existing_duration_tag(tmp_path):
    from copy import deepcopy
    store, fake, b, _ = board(tmp_path)
    fake.tags.append("15-минут")
    snap = deepcopy(SNAP)
    snap["human"]["actions"][0].update(deadline="2026-10-22T06:30:00+00:00", duration_tag="15-минут")
    b.sync(snap, system_lines=["x"])
    question = next(t for t in fake.tasks.values() if t["kind"] == "CHECKLIST")
    assert question["dueDate"] == "2026-10-22T09:30:00+03:00"
    assert question["timeZone"] == "Europe/Moscow"
    assert "15-минут" in question["tags"]
    assert not question.get("reminders")


def test_owner_tick_records_trusted_ops_approval_for_that_plan(tmp_path):
    store, fake, b, root = board(tmp_path)
    b.sync(SNAP, system_lines=["x"])
    question = next(t for t in fake.tasks.values() if t["kind"] == "CHECKLIST")
    question["items"][0]["status"] = 1  # the owner ticks «Да»
    b.sync(SNAP, system_lines=["x"])
    with store.read() as tx:
        rows = tx.query("SELECT payload FROM events WHERE kind='owner.ticktick_ops_requested'")
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='intent.ops_approved'") == 0
    payload = json.loads(rows[0]["payload"])
    assert payload["authorization"] == "operator_required" and "approved_by" not in payload
    assert payload["plan_sha256"] == hashlib.sha256("план".encode()).hexdigest()
    assert question["status"] == 2 and any("для запуска подтвердите этот план оператору" in c["title"] for c in fake.comments)


def test_two_ticks_decide_nothing_and_warn_once(tmp_path):
    store, fake, b, _ = board(tmp_path)
    b.sync(SNAP, system_lines=["x"])
    question = next(t for t in fake.tasks.values() if t["kind"] == "CHECKLIST")
    for item in question["items"]:
        item["status"] = 1
    b.sync(SNAP, system_lines=["x"])
    b.sync(SNAP, system_lines=["x"])
    with store.read() as tx:
        assert not tx.query("SELECT 1 FROM events WHERE kind='intent.ops_approved'")
    assert sum("несколько вариантов" in c["title"] for c in fake.comments) == 1


def test_owner_tags_survive_state_tag_changes(tmp_path):
    store, fake, b, _ = board(tmp_path)
    b.sync(SNAP, system_lines=["x"])
    card = next(t for t in fake.tasks.values() if t["kind"] == "TEXT")
    card["tags"].append("Zettelkasten")
    quiet = {"status": "ready", "human": {"projects": SNAP["human"]["projects"], "actions": [],
                                          "fleet": {}}}
    b.sync(quiet, system_lines=["x"])
    assert "Zettelkasten" in card["tags"] and "нужно-решение" not in card["tags"]


def test_oauth_credential_fails_closed_and_never_leaks(tmp_path):
    path = tmp_path / "secrets" / "ticktick-oauth.json"
    with pytest.raises(oauth.TickTickAuthError):
        oauth.credential(path)
    oauth.save({"access_token": "SECRET-X", "expires_at": 10.0}, path)
    with pytest.raises(oauth.TickTickAuthError) as err:
        oauth.credential(path, now=lambda: 100.0)
    assert "SECRET-X" not in str(err.value)
    oauth.save({"access_token": "SECRET-X", "expires_at": 10_000.0}, path)
    assert oauth.credential(path, now=lambda: 100.0) == "SECRET-X"
    assert oauth.status(path, now=lambda: 100.0)["connected"] is True


def test_oauth_rejects_foreign_endpoints_and_bad_state():
    with pytest.raises(oauth.TickTickAuthError):
        oauth._trusted("https://evil.example/oauth/token")
    with pytest.raises(oauth.TickTickAuthError):
        oauth.parse_callback("/callback?code=1&state=other", "mine")
    assert oauth.parse_callback("/callback?code=abc&state=mine", "mine") == "abc"


def test_task_titles_drop_ids_and_sections_regroup_without_new_columns(tmp_path):
    assert tb._human_task("Fix request:9b80c113") == ""
    assert tb._human_task("Review TZ-403") == ""
    assert tb._human_task("Стадия hypotheses_plan") == ""
    assert tb._human_task("Request: TZ-506 — типографская иконка вопроса") == "типографская иконка вопроса"
    store, fake, b, _ = board(tmp_path)
    b.sync(SNAP, system_lines=["x"])
    columns_before = len(fake.columns)
    quiet = {"status": "ready", "human": {"projects": [], "actions": [], "fleet": {}}}
    b.sync(quiet, system_lines=["x"])  # the intent section leaves the board
    b.sync(SNAP, system_lines=["x"])   # and comes back: the parked column is reused
    assert len(fake.columns) == columns_before


def test_intent_title_uses_the_owners_own_wording(tmp_path):
    (tmp_path / "a.md").write_text("# Интенция владельца №17 — вернуть к работе «Portfolio Export»\n",
                                   encoding="utf-8")
    (tmp_path / "b.md").write_text("Интенция владельца (пункт 12), дословно:\n\n"
                                   "возможно стоит сделать Deep Research. Дальше\n", encoding="utf-8")
    assert tb.intent_title("a.md", tmp_path) == "Вернуть к работе «Portfolio Export»"
    assert tb.intent_title("b.md", tmp_path) == "Возможно стоит сделать Deep Research."


def test_owner_subtask_becomes_fleet_request_and_withdraws_when_ticked(tmp_path):
    store, fake, b, root = board(tmp_path)
    b.sync(SNAP, system_lines=["x"])
    card = next(t for t in fake.tasks.values() if t["kind"] == "TEXT")
    mine = fake.call("create_task", {"task": {"projectId": card["projectId"], "parentId": card["id"],
                                              "kind": "TEXT", "title": "Сравнить две модели"}})
    b.sync(SNAP, system_lines=["x"])
    with store.read() as tx:
        rows = tx.query("SELECT tz,state,body FROM tasks WHERE project='second-pc-intent-x' AND tz LIKE 'request:%' "
                        "AND body LIKE '%Сравнить%'")
    assert len(rows) == 1 and "Сравнить две модели" in rows[0]["body"]
    assert "Сравнить две модели" in (root / "intent" / "owner-notes.md").read_text(encoding="utf-8")
    assert "в-работе" in mine["tags"]
    b.sync(SNAP, system_lines=["x"])  # idempotent
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE project='second-pc-intent-x' "
                         "AND tz LIKE 'request:%' AND body LIKE '%Сравнить%'") == 1
    mine["status"] = 2  # the owner ticks it before the fleet is done
    b.sync(SNAP, system_lines=["x"])
    with store.read() as tx:
        assert tx.scalar("SELECT state FROM tasks WHERE tz=?", (rows[0]["tz"],)) == "frozen"


def test_now_note_title_carries_the_meaning(tmp_path):
    section = {"intents": [{"card": {"stages": [{"done": True}, {"done": False}, {"done": False}]}}],
               "actions": [1], "running": ["a"], "checking": [], "ready": ["b", "c"], "waiting": ["d"]}
    assert tb.now_title(section) == "Сейчас: этап 2 из 3, нужно ваше решение, работает агентов: 1, 2 в очереди"


def test_card_explains_itself_from_owner_brief_and_brief_is_requested_otherwise(tmp_path):
    store, fake, b, root = board(tmp_path)
    b.sync(SNAP, system_lines=["x"])
    with store.read() as tx:
        asked = tx.query("SELECT acceptance FROM tasks WHERE body LIKE '%простыми словами%'")
    assert len(asked) == 1 and ".intent/check.py owner_brief" in asked[0]["acceptance"]
    (root / "intent" / "owner-brief.md").write_text(
        "## Коротко\nПроверяем, может ли второй компьютер сам отвечать вместо платного сервиса. "
        "Если да — будет дешевле.\n## Вопросы\n- Одобрить операционный план с откатом: ставим "
        "программу на второй компьютер; если «Да» — флот установит; если «Нет» — оставим как есть.\n",
        encoding="utf-8")
    b.sync(SNAP, system_lines=["x"])
    card = next(t for t in fake.tasks.values() if t["kind"] == "TEXT" and not t.get("parentId"))
    assert card["content"].startswith("Проверяем, может ли второй компьютер")
    question = next(t for t in fake.tasks.values() if t["kind"] == "CHECKLIST")
    assert question["desc"].startswith("Одобрить операционный план с откатом: ставим программу")


def _owner_card(fake, b, title, column_key="inbox", content="", **extra):
    return fake.call("create_task", {"task": {"projectId": b.state["list_id"],
                                              "columnId": b.state["columns"][column_key],
                                              "kind": "TEXT", "title": title, "content": content, **extra}})


def test_owner_card_becomes_intent_file_and_edits_make_a_revision(tmp_path):
    from dev_mcp.fleet import controller
    store, fake, b, _ = board(tmp_path)
    b.sync(SNAP, system_lines=["x"])
    card = _owner_card(fake, b, "Сделать бота для заметок", content="Хочу голосом.")
    b.sync(SNAP, system_lines=["x"])
    files = list(controller.INTENT_INBOX.glob("*.md"))
    assert [f.name for f in files] == ["sdelat-bota-dlya-zametok.md"]
    text = files[0].read_text(encoding="utf-8")
    assert text.startswith("# Сделать бота для заметок") and "Хочу голосом." in text
    assert any("принял, разбираюсь" in c["title"] for c in fake.comments if c["task"] == card["id"])
    assert card["title"] == "Сделать бота для заметок"  # the owner's card is never retitled
    card["content"] = "Хочу голосом и с телефона."
    b.sync(SNAP, system_lines=["x"])
    assert "с телефона" in files[0].read_text(encoding="utf-8")
    assert any("вижу правку" in c["title"] for c in fake.comments if c["task"] == card["id"])


def test_card_in_a_project_section_says_which_project(tmp_path):
    from dev_mcp.fleet import controller
    store, fake, b, _ = board(tmp_path)
    b.sync(SNAP, system_lines=["x"])
    key = next(k for k in b.state["columns"] if k.startswith("s:") and "Beta App" in k)
    _owner_card(fake, b, "Ускорить запуск", column_key=key)
    b.sync(SNAP, system_lines=["x"])
    text = (controller.INTENT_INBOX / "uskorit-zapusk.md").read_text(encoding="utf-8")
    assert "раздел «Beta App»" in text


def test_owner_move_revises_section_and_inbox_keeps_the_card_paused(tmp_path):
    from dev_mcp.fleet import controller
    store, fake, b, _ = board(tmp_path)
    b.sync(SNAP, system_lines=["x"])
    card = _owner_card(fake, b, "Move canary")
    b.sync(SNAP, system_lines=["x"])
    rec = b.state["owner_cards"][card["id"]]
    key = next(k for k in b.state["columns"] if k.startswith("s:") and "Beta App" in k)
    card["columnId"] = b.state["columns"][key]
    b.sync(SNAP, system_lines=["x"])
    assert "раздел «Beta App»" in (controller.INTENT_INBOX / rec["file"]).read_text(encoding="utf-8")
    card["columnId"] = b.state["columns"]["inbox"]
    b.sync(SNAP, system_lines=["x"])
    b.sync(SNAP, system_lines=["x"])
    assert rec["state"] == "paused" and rec["why"] == "inbox"
    card["columnId"] = b.state["columns"][key]
    b.sync(SNAP, system_lines=["x"])
    assert rec["state"] == "open"


def test_tag_anywhere_pulls_the_task_onto_the_board(tmp_path):
    from dev_mcp.fleet import controller
    store, fake, b, _ = board(tmp_path)
    b.sync(SNAP, system_lines=["x"])
    elsewhere = fake.call("create_task", {"task": {"projectId": "inbox", "kind": "TEXT",
                                                   "title": "Разобрать почту", "tags": ["флот"]}})
    b.sync(SNAP, system_lines=["x"])
    assert elsewhere["projectId"] == b.state["list_id"]
    assert elsewhere["columnId"] == b.state["columns"]["inbox"]
    assert (controller.INTENT_INBOX / "razobrat-pochtu.md").is_file()


@pytest.mark.parametrize("include_closed", [False, True])
def test_closing_pauses_and_reopening_resumes(tmp_path, include_closed):
    store, fake, b, _ = board(tmp_path)
    b.sync(SNAP, system_lines=["x"])
    if include_closed:
        original_call = fake.call
        def call(name, args):
            if name == "filter_tasks" and args["filter"].get("projectIds"):
                return list(fake.tasks.values())
            return original_call(name, args)
        fake.call = call
    card = _owner_card(fake, b, "Новая затея")
    b.sync(SNAP, system_lines=["x"])
    rec = b.state["owner_cards"][card["id"]]
    b.state["cards"]["owner-bound-project"] = card["id"]
    card["status"] = -1  # «Не буду делать»
    b.sync(SNAP, system_lines=["x"])
    assert rec["state"] == "paused" and rec["why"] == "wont_do"
    card["status"] = 0
    card["content"] = "Edited after project binding"
    b.sync(SNAP, system_lines=["x"])
    assert rec["state"] == "open"
    from dev_mcp.fleet.controller import INTENT_INBOX
    assert "Edited after project binding" in (INTENT_INBOX / rec["file"]).read_text(encoding="utf-8")
    assert any("продолжаю" in c["title"] for c in fake.comments if c["task"] == card["id"])


def test_deletion_needs_two_misses_before_pausing(tmp_path):
    store, fake, b, _ = board(tmp_path)
    b.sync(SNAP, system_lines=["x"])
    card = _owner_card(fake, b, "Хрупкая затея")
    b.sync(SNAP, system_lines=["x"])
    del fake.tasks[card["id"]]
    b.sync(SNAP, system_lines=["x"])
    assert b.state["owner_cards"][card["id"]]["state"] == "open"
    b.sync(SNAP, system_lines=["x"])
    assert b.state["owner_cards"][card["id"]]["state"] == "paused"


def test_owner_comment_reaches_the_stage_agent_and_notes(tmp_path):
    store, fake, b, root = board(tmp_path)
    with store.tx() as tx:
        tx.change("INSERT INTO workers(id,label,first_seen,last_seen) VALUES('W1','w','t','t')", event="seed")
        tx.change("INSERT INTO tasks(project,tz,title,state,created_at,updated_at) "
                  "VALUES('second-pc-intent-x','stage','Стадия','taken','t','t')", event="seed")
        tx.change("INSERT INTO claims(project,tz,worker_id,state,started_at) "
                  "VALUES('second-pc-intent-x','stage','W1','active','t')", event="seed")
    b.sync(SNAP, system_lines=["x"])
    card_id = b.state["cards"]["second-pc-intent-x"]
    fake.comments.append({"id": "owner-c1", "task": card_id, "title": "Только без облака, пожалуйста"})
    b.sync(SNAP, system_lines=["x"])
    with store.read() as tx:
        msgs = tx.query("SELECT to_task,body FROM messages")
    assert [(m["to_task"], m["body"]) for m in msgs] == [
        ("second-pc-intent-x/stage", "Владелец написал в TickTick: Только без облака, пожалуйста")]
    assert "без облака" in (root / "intent" / "owner-notes.md").read_text(encoding="utf-8")
    b.sync(SNAP, system_lines=["x"])  # delivered once
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM messages") == 1


DONE_SNAP = {"status": "ready", "human": {
    "projects": [], "actions": [],
    "done": [{"project": "second-pc-intent-x", "title": "Локальный ИИ на втором ПК",
              "text": "**Работает**: второй компьютер отвечает вместо платного сервиса, commit 214e48eb9a0fda1cbc94"}],
    "fleet": {"agents": 1, "queue": 0}}}


def test_handed_over_card_waits_for_acceptance_and_the_tick_closes_it(tmp_path):
    store, fake, b, _ = board(tmp_path)
    b.sync(DONE_SNAP, system_lines=["x"])
    card = next(t for t in fake.tasks.values() if t["kind"] == "TEXT" and not t.get("parentId"))
    assert "на-приёмку" in card["tags"]
    assert card["content"].startswith("Готово: Работает: второй компьютер")
    assert "**" not in card["content"] and "214e48" not in card["content"]
    told = [c for c in fake.comments if c["task"] == card["id"] and "готово:" in c["title"]]
    b.sync(DONE_SNAP, system_lines=["x"])
    assert len(told) == 1 and len([c for c in fake.comments if c["task"] == card["id"]
                                   and "готово:" in c["title"]]) == 1  # announced once
    card["status"] = 2  # the owner accepts
    b.sync(DONE_SNAP, system_lines=["x"])
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='owner.ticktick_accepted'") == 1
    assert any("принято вами" in c["title"] for c in fake.comments if c["task"] == card["id"])
    cards = len([t for t in fake.tasks.values() if t["kind"] == "TEXT" and not t.get("parentId")])
    b.sync(DONE_SNAP, system_lines=["x"])  # accepted: no new card for the same result
    assert len([t for t in fake.tasks.values() if t["kind"] == "TEXT" and not t.get("parentId")]) == cards


def test_early_tick_on_a_fleet_card_pauses_and_unticking_resumes(tmp_path):
    store, fake, b, _ = board(tmp_path)
    with store.tx() as tx:
        tx.change("INSERT INTO tasks(project,tz,title,state,created_at,updated_at) "
                  "VALUES('second-pc-intent-x','TZ-1','Поставить модель','ready','t','t')", event="seed")
    b.sync(SNAP, system_lines=["x"])
    card = next(t for t in fake.tasks.values() if t["kind"] == "TEXT" and not t.get("parentId"))
    card["status"] = 2
    b.sync(SNAP, system_lines=["x"])
    with store.read() as tx:
        assert tx.scalar("SELECT state FROM tasks WHERE tz='TZ-1'") == "frozen"
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='owner.ticktick_accepted'") == 0
    assert "на-паузе" in card["tags"]
    card["status"] = 0
    b.sync(SNAP, system_lines=["x"])
    with store.read() as tx:
        assert tx.scalar("SELECT state FROM tasks WHERE tz='TZ-1'") == "ready"


def test_owner_card_result_is_announced_and_its_tick_accepts(tmp_path):
    store, fake, b, _ = board(tmp_path)
    b.sync(SNAP, system_lines=["x"])
    card = _owner_card(fake, b, "Локальный ИИ")
    b.sync(SNAP, system_lines=["x"])
    rec = b.state["owner_cards"][card["id"]]
    rec["project"] = "second-pc-intent-x"  # the pipeline has made the intent project
    b.state["cards"]["second-pc-intent-x"] = card["id"]
    snap = json.loads(json.dumps(DONE_SNAP))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tb, "intent_title", lambda *a, **k: "Локальный ИИ")
        mp.setattr(b, "_ensure_card", lambda intent, column: card["id"])
        b.sync(snap, system_lines=["x"])
        assert card["title"] == "Локальный ИИ" and "на-приёмку" in card["tags"]
        assert any("готово:" in c["title"] for c in fake.comments if c["task"] == card["id"])
        card["status"] = 2
        b.sync(snap, system_lines=["x"])
    assert rec["state"] == "accepted"
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='owner.ticktick_accepted'") == 1


def test_owner_subtask_goes_to_acceptance_when_its_work_is_done(tmp_path):
    store, fake, b, _ = board(tmp_path)
    b.sync(SNAP, system_lines=["x"])
    card = next(t for t in fake.tasks.values() if t["kind"] == "TEXT")
    mine = fake.call("create_task", {"task": {"projectId": card["projectId"], "parentId": card["id"],
                                              "kind": "TEXT", "title": "Сравнить две модели"}})
    b.sync(SNAP, system_lines=["x"])
    item = b.state["owner_items"][mine["id"]]
    with store.tx() as tx:  # the request ran 16:00–16:20 and drafted TZ-7, which was then done
        tx.change("UPDATE tasks SET state='accepted', created_at='2026-10-19T16:00:00+00:00', "
                  "updated_at='2026-10-19T16:20:00+00:00' WHERE project=? AND tz=?",
                  ("second-pc-intent-x", item["tz"]), event="seed")
        tx.change("INSERT INTO tasks(project,tz,title,state,created_at,updated_at) VALUES("
                  "'second-pc-intent-x','TZ-7','TZ-7: Сравнение Qwen и Llama','taken','t','t')", event="seed")
        tx.change("INSERT INTO events(ts,kind,project,tz,payload) VALUES("
                  "'2026-10-19T16:25:00+00:00','draft_registered','second-pc-intent-x','TZ-7','{}')",
                  event="seed")
        tx.change("INSERT INTO events(ts,kind,project,tz,payload) VALUES("
                  "'2026-10-19T12:00:00+00:00','draft_registered','second-pc-intent-x','TZ-0','{}')",
                  event="seed")
    b.sync(SNAP, system_lines=["x"])
    assert item["work"] == ["TZ-7"] and not item.get("review")
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE tz='TZ-7'", event="seed")
    b.sync(SNAP, system_lines=["x"])
    assert item["review"] and "на-приёмку" in mine["tags"]
    assert any("Сравнение Qwen и Llama" in c["title"] for c in fake.comments if c["task"] == mine["id"])
    mine["status"] = 2  # the owner accepts — not a withdrawal
    b.sync(SNAP, system_lines=["x"])
    assert item["state"] == "accepted"
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='owner.ticktick_item_accepted'") == 1


def test_live_bucket_is_a_monitor_not_a_queue_count():
    from datetime import timedelta
    now = datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)
    fresh, stale = (now - timedelta(minutes=2)).isoformat(), (now - timedelta(minutes=20)).isoformat()
    states = {("P", "A"): "accepted", ("P", "B"): "taken", ("P", "S"): "submitted",
              ("P", "review:S:2"): "frozen", ("P", "S2"): "submitted", ("P", "review:S2"): "taken"}
    seen = {("P", "B"): fresh, ("P", "C"): stale}
    row = lambda tz, state, needs="[]", upd=now.isoformat(): {
        "project": "P", "tz": tz, "state": state, "needs": needs, "updated_at": upd}
    assert tb.live_bucket(row("B", "taken"), states, seen, now) == "running"
    assert tb.live_bucket(row("C", "taken"), states, seen, now) == "silent"
    assert tb.live_bucket(row("S", "submitted"), states, seen, now) == "stuck"   # its review is frozen
    assert tb.live_bucket(row("S2", "submitted"), states, seen, now) == "checking"
    assert tb.live_bucket(row("R", "ready", '["A", "A#contract"]'), states, seen, now) == "ready"
    assert tb.live_bucket(row("W", "ready", '["B"]'), states, seen, now) == "waiting"
    assert tb.live_bucket(row("F", "frozen"), states, seen, now) == "stuck"
    assert tb.live_bucket(row("D", "ready", '["NOPE"]'), states, seen, now) == "stuck"  # waits for nothing
    old = (now - timedelta(days=30)).isoformat()
    assert tb.live_bucket(row("O", "frozen", upd=old), states, seen, now) is None  # history, not a jam
    section = {"running": [], "checking": [], "ready": ["x"], "stuck": []}
    assert tb.state_tag({"actions": [], "load": {"running": 0, "ready": 0, "stuck": 1}}, section) == "застряло"
    assert tb.state_tag({"actions": [], "load": {"running": 1}}, section) == "в-работе"
    assert tb.state_tag({"actions": [], "load": {}}, section) == "ждёт-условий"  # a busy section is not this card
