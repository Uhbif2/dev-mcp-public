from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet import jev, judgment, lessons
from dev_mcp.fleet.store import Store

LONG = "Содержательный дефект при зелёной приёмке: " + "x" * 80


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@pytest.fixture()
def store(tmp_path: Path, monkeypatch) -> Store:
    monkeypatch.setattr(jev, "configured", lambda: True)
    s = Store.open(str(tmp_path / "fleet.sqlite"))
    root = tmp_path / "P"
    (root / "ToDo").mkdir(parents=True)
    with s.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES('P',?,1,1)", (str(root),),
                  event={"kind": "test_setup"})
    return s


def add_task(store: Store, tz: str, *, acceptance: bool = True, kind: str = "work") -> None:
    with store.tx() as tx:
        tx.change(
            "INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,resources,"
            "acceptance,body,created_at,updated_at) VALUES('P',?,?,'ready',1,?,'','','[]','[]','[]',?,'',?,?)",
            (tz, f"задача {tz}", kind, json.dumps(["pytest"] if acceptance else []), now(), now()),
            event={"kind": "test_setup"},
        )


def ev(store: Store, kind: str, tz: str, **payload) -> None:
    with store.tx() as tx:
        tx.record_event(kind=kind, project="P", tz=tz, worker_id=payload.pop("worker_id", None), payload=payload)


def scans(store: Store) -> list[dict]:
    with store.read() as tx:
        return [json.loads(r["payload"]) for r in tx.query("SELECT payload FROM events WHERE kind='lesson_scan' ORDER BY id")]


def fake(g: float = 0.9, choice: str | None = None, p: float = 0.9):
    calls: list[dict] = []

    def ask(state, questions, **_kw):
        calls.append({"state": state, "questions": questions})
        out = {"g": {"noul": g}}
        if "c" in questions and choice:
            out["c"] = {"probabilities": {choice: p, "none": 1 - p}}
        elif "c" in questions:
            out["c"] = {"probabilities": {"none": 0.9}}
        for name in questions:
            if name not in ("g", "c"):
                out[name] = {"noul": g}
        return out

    ask.calls = calls  # type: ignore[attr-defined]
    return ask


LESSON_MD = """# урок
```yaml
id: input-domain
class: код принимает значения вне заявленной области вместо отказа
rule: Проверяй вход по заявленной области и отказывай без изменения состояния.
check: подай пустое и неканоническое значение — состояние не меняется
trigger: задача трогает разбор или приём внешних значений
tier: briefing
scope: project
evidence: [11, 12]
```
"""


def make_lesson(store: Store, tmp_path: Path, name: str = "input-domain", **over) -> None:
    text = LESSON_MD.replace("input-domain", name)
    for key, value in over.items():
        text = "\n".join(f"{key}: {value}" if line.startswith(f"{key}:") else line for line in text.splitlines())
    path = tmp_path / f"{name}.md"
    path.write_text(text, encoding="utf-8")
    ids, errors = lessons.register_files(store, "P", [path], "owner")
    assert ids == [name], errors


# ---- разбор отказов ----------------------------------------------------------------------------

def test_scan_sleeps_without_key(store: Store, monkeypatch) -> None:
    monkeypatch.setattr(jev, "configured", lambda: False)
    add_task(store, "TZ-1")
    ev(store, "reject", "TZ-1", note=LONG)
    assert lessons.scan(store, fake()) == 0 and scans(store) == []


def test_scan_takes_only_rejects_of_tasks_with_acceptance(store: Store) -> None:
    add_task(store, "TZ-1")
    add_task(store, "TZ-2", acceptance=False)
    add_task(store, "review:TZ-1", kind="review")
    ev(store, "reject", "TZ-1", note=LONG)
    ev(store, "reject", "TZ-2", note=LONG)
    ev(store, "reject", "review:TZ-1", note=LONG)
    ev(store, "reject", "TZ-1", note="коротко")
    assert lessons.scan(store, fake()) == 2
    got = scans(store)
    assert [bool(s.get("skip")) for s in got] == [False, True]
    assert lessons.scan(store, fake()) == 0  # отметка прошла: повторно не разбираем


def test_scan_known_class_is_recorded_and_unsure_is_not(store: Store, tmp_path: Path) -> None:
    add_task(store, "TZ-1")
    make_lesson(store, tmp_path)
    ev(store, "reject", "TZ-1", note=LONG)
    lessons.scan(store, fake(choice="input-domain", p=0.95))
    assert scans(store)[0]["lesson"] == "input-domain"
    ev(store, "reject", "TZ-1", note=LONG)
    lessons.scan(store, fake(choice="input-domain", p=0.6))
    assert scans(store)[1]["lesson"] is None


def test_scan_stops_when_oracle_does_not_answer(store: Store) -> None:
    add_task(store, "TZ-1")
    ev(store, "reject", "TZ-1", note=LONG)
    assert lessons.scan(store, lambda *a, **k: None) == 0
    assert scans(store) == []  # повторит на следующем тике


# ---- пул и майнер ------------------------------------------------------------------------------

def fill_pool(store: Store, n: int) -> None:
    add_task(store, "TZ-1")
    for _ in range(n):
        ev(store, "reject", "TZ-1", note=LONG)
    lessons.scan(store, fake(), limit=50)


def test_pool_opens_one_mining_triage_and_is_consumed(store: Store) -> None:
    fill_pool(store, lessons.POOL_N - 1)
    assert lessons.open_mining(store) == []
    ev(store, "reject", "TZ-1", note=LONG)
    lessons.scan(store, fake())
    assert lessons.open_mining(store) == ["P/triage:1"]
    with store.read() as tx:
        task = dict(tx.one("SELECT owns,body FROM tasks WHERE kind='triage'"))
        assert lessons.pool(tx, "P") == []
    assert json.loads(task["owns"])[0].endswith("/todo/lessons/**")
    assert "subject=lessons:P" in task["body"] and "ToDo/lessons/<id>.md" in task["body"]
    assert lessons.open_mining(store) == []


def test_pool_returns_cases_when_miner_closes_without_apply(store: Store) -> None:
    fill_pool(store, lessons.POOL_N)
    assert lessons.open_mining(store) == ["P/triage:1"]
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='done' WHERE kind='triage'", (), event={"kind": "test_setup"})
    with store.read() as tx:
        assert len(lessons.pool(tx, "P")) == lessons.POOL_N
    with store.tx() as tx:
        tx.record_event(kind="triage_applied", project="P", tz="triage:1", worker_id=None,
                        payload={"subject": "lessons:P"})
    with store.read() as tx:
        assert lessons.pool(tx, "P") == []


def test_accepted_mining_consumes_only_selected_20_and_preserves_four_unreviewed(
    store: Store,
) -> None:
    # The first miner receives only the latest MINING_NOTES=20 of 24 rejects.
    # Accept consumes ONLY those twenty: suppressing the other four would
    # permanently discard rejects that the miner never saw.
    fill_pool(store, 24)
    with store.read() as tx:
        original = [item["ev"] for item in lessons.pool(tx, "P")]
        assert len(original) == 24 > lessons.MINING_NOTES
    assert lessons.open_mining(store) == ["P/triage:1"]
    with store.tx() as tx:
        open_event = tx.one("SELECT payload FROM events WHERE kind='triage_open'")
        selected = json.loads(open_event["payload"])["pool"]
        assert len(selected) == lessons.MINING_NOTES
        tx.change(
            "UPDATE tasks SET state='accepted' WHERE project='P' AND tz='triage:1'",
            event={"kind": "test_accept"},
        )
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='triage_applied'") == 0
    with store.read() as tx:
        waiting = [item["ev"] for item in lessons.pool(tx, "P")]
        assert waiting == original[:-lessons.MINING_NOTES]
        assert not set(waiting).intersection(selected)
    assert lessons.open_mining(store) == []
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE kind='triage'") == 1


def test_unreviewed_backlog_and_one_new_reject_reopen_after_cooldown(
    store: Store,
) -> None:
    fill_pool(store, 24)
    assert lessons.open_mining(store) == ["P/triage:1"]
    with store.tx() as tx:
        tx.change(
            "UPDATE tasks SET state='accepted' WHERE project='P' AND tz='triage:1'",
            event={"kind": "test_accept"},
        )
    with store.read() as tx:
        waiting = [item["ev"] for item in lessons.pool(tx, "P")]
        assert len(waiting) == 4
    later = datetime.now(timezone.utc) + timedelta(hours=lessons.MINING_COOLDOWN_H + 1)
    assert lessons.open_mining(store, now=later) == []
    ev(store, "reject", "TZ-1", note=LONG)
    assert lessons.scan(store, fake()) == 1
    with store.read() as tx:
        pool = lessons.pool(tx, "P")
        assert len(pool) == lessons.POOL_N
        assert [item["ev"] for item in pool[:-1]] == waiting
        assert pool[-1]["ev"] > max(waiting)
    assert lessons.open_mining(store, now=later) == ["P/triage:2"]
    with store.read() as tx:
        triage = tx.one("SELECT payload FROM events WHERE kind='triage_open' ORDER BY id DESC LIMIT 1")
        assert json.loads(triage["payload"])["pool"] == [item["ev"] for item in pool]
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE kind='triage'") == 2
    assert lessons.open_mining(store, now=later) == []


def test_accepted_mining_fingerprint_is_durable_after_reimport(store: Store) -> None:
    fill_pool(store, 24)
    with store.read() as tx:
        source = [item["ev"] for item in lessons.pool(tx, "P")]
    assert lessons.open_mining(store) == ["P/triage:1"]
    with store.read() as tx:
        record = tx.one("SELECT payload FROM events WHERE kind='triage_open' AND tz='triage:1'")
        first = json.loads(record["payload"])
    selected = source[-lessons.MINING_NOTES:]
    assert first["pool"] == selected
    assert first["pool_fingerprint"] == hashlib.sha256(
        ",".join(str(i) for i in sorted(selected)).encode("ascii")
    ).hexdigest()
    # Durable review verdict, not mutable tasks.state, is authoritative.
    ev(store, "accept", "triage:1", worker_id="W-review-A", review_id="review:triage:1:42",
       note="independent review accepted")
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='done' WHERE project='P' AND tz='triage:1'",
                  event={"kind": "test_reimport"})
    with store.read() as tx:
        assert [item["ev"] for item in lessons.pool(tx, "P")] == source[:-lessons.MINING_NOTES]
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='triage_applied'") == 0
    later = datetime.now(timezone.utc) + timedelta(hours=lessons.MINING_COOLDOWN_H + 1)
    assert lessons.open_mining(store, now=later) == []
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM tasks WHERE kind='triage'") == 1


def test_new_reject_after_durable_accept_opens_one_distinct_fingerprint(store: Store) -> None:
    fill_pool(store, 24)
    with store.read() as tx:
        original = [item["ev"] for item in lessons.pool(tx, "P")]
    assert lessons.open_mining(store) == ["P/triage:1"]
    with store.read() as tx:
        first = json.loads(tx.one(
            "SELECT payload FROM events WHERE kind='triage_open' AND tz='triage:1'"
        )["payload"])
    ev(store, "accept", "triage:1", worker_id="another-reviewer",
       review_id="review:triage:1:999", note="different prose, same source events")
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='done' WHERE project='P' AND tz='triage:1'",
                  event={"kind": "test_reimport"})
    later = datetime.now(timezone.utc) + timedelta(hours=lessons.MINING_COOLDOWN_H + 1)
    assert lessons.open_mining(store, now=later) == []
    ev(store, "reject", "TZ-1", worker_id="W-new", note=LONG + " altered text")
    assert lessons.scan(store, fake()) == 1
    with store.read() as tx:
        expected = [item["ev"] for item in lessons.pool(tx, "P")]
    assert expected[:-1] == original[:-lessons.MINING_NOTES]
    assert len(expected) == lessons.POOL_N and expected[-1] > max(original)
    assert lessons.open_mining(store, now=later) == ["P/triage:2"]
    with store.read() as tx:
        second = json.loads(tx.one(
            "SELECT payload FROM events WHERE kind='triage_open' AND tz='triage:2'"
        )["payload"])
    assert second["pool"] == expected
    assert second["pool_fingerprint"] != first["pool_fingerprint"]
    assert second["pool_fingerprint"] == lessons._source_fingerprint(list(reversed(expected)))
    assert lessons.open_mining(store, now=later) == []


def test_closed_mining_without_accepted_review_remains_retryable(store: Store) -> None:
    fill_pool(store, lessons.POOL_N)
    assert lessons.open_mining(store) == ["P/triage:1"]
    with store.read() as tx:
        original = json.loads(tx.one(
            "SELECT payload FROM events WHERE kind='triage_open' AND tz='triage:1'"
        )["payload"])
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='done' WHERE project='P' AND tz='triage:1'",
                  event={"kind": "test_closed_without_accept"})
    later = datetime.now(timezone.utc) + timedelta(hours=lessons.MINING_COOLDOWN_H + 1)
    assert lessons.open_mining(store, now=later) == ["P/triage:2"]
    with store.read() as tx:
        retry = json.loads(tx.one(
            "SELECT payload FROM events WHERE kind='triage_open' AND tz='triage:2'"
        )["payload"])
    assert retry["pool"] == original["pool"]
    assert retry["pool_fingerprint"] == original["pool_fingerprint"]
    assert lessons.open_mining(store, now=later) == []  # same active subject


def test_fifth_genuinely_new_reject_reopens_after_full_batch_accepted(
    store: Store,
) -> None:
    # With exactly MINING_NOTES old rejects there is NO historic backlog.
    fill_pool(store, lessons.MINING_NOTES)
    assert lessons.open_mining(store) == ["P/triage:1"]
    with store.tx() as tx:
        tx.change(
            "UPDATE tasks SET state='accepted' WHERE project='P' AND tz='triage:1'",
            event={"kind": "test_accept"},
        )
    with store.read() as tx:
        assert lessons.pool(tx, "P") == []
    later = datetime.now(timezone.utc) + timedelta(hours=lessons.MINING_COOLDOWN_H + 1)
    for _ in range(lessons.POOL_N - 1):
        ev(store, "reject", "TZ-1", note=LONG)
    assert lessons.scan(store, fake(), limit=50) == lessons.POOL_N - 1
    assert lessons.open_mining(store, now=later) == []
    ev(store, "reject", "TZ-1", note=LONG)
    assert lessons.scan(store, fake()) == 1
    with store.read() as tx:
        new_pool = lessons.pool(tx, "P")
        assert len(new_pool) == lessons.POOL_N
    assert lessons.open_mining(store, now=later) == ["P/triage:2"]


def test_one_off_rejects_do_not_reach_the_miner(store: Store) -> None:
    add_task(store, "TZ-1")
    for _ in range(lessons.POOL_N + 2):
        ev(store, "reject", "TZ-1", note=LONG)
    lessons.scan(store, fake(g=0.1), limit=50)
    assert lessons.open_mining(store) == []


def test_accepted_triage_registers_lesson_files(store: Store, tmp_path: Path) -> None:
    fill_pool(store, lessons.POOL_N)
    lessons.open_mining(store)
    root = tmp_path / "P"
    assert (root / "ToDo" / "lessons").is_dir()  # майнер заранее создаёт зону записи
    good = root / "ToDo" / "lessons" / "input-domain.md"
    good.write_text(LESSON_MD, encoding="utf-8")
    bad = root / "ToDo" / "lessons" / "broken.md"
    bad.write_text("```yaml\nid: x\nclass: коротко\n```", encoding="utf-8")
    none = root / "ToDo" / "lessons" / "_none.md"
    none.write_text("```yaml\nclass: none\n```", encoding="utf-8")
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE kind='triage'", (), event={"kind": "test_setup"})
        for p in (good, bad, none):
            tx.record_event(kind="write", project="P", tz="triage:1", path_norm=str(p).replace("\\", "/").lower())
    done = judgment.apply_accepted(store, sync=lambda *a, **k: {})
    assert done[0]["lessons"] == ["input-domain"]
    assert any("broken.md" in e for e in done[0]["errors"])
    with store.read() as tx:
        assert [x["id"] for x in lessons.catalog(tx, "P")] == ["input-domain"]


def test_parse_rejects_malformed(tmp_path: Path) -> None:
    path = tmp_path / "x.md"
    path.write_text("```yaml\nid: Bad Id\nclass: короткий\ntier: magic\n```", encoding="utf-8")
    with pytest.raises(ValueError) as err:
        lessons.parse_lesson(path)
    assert "id:" in str(err.value) and "tier:" in str(err.value)


# ---- памятка -----------------------------------------------------------------------------------

def task(tz: str = "TZ-9", kind: str = "work") -> SimpleNamespace:
    return SimpleNamespace(project="P", tz=tz, kind=kind, title="разбор входных значений", body="",
                           owns=("a.py",), acceptance=("pytest",))


def test_brief_shows_relevant_lessons_and_is_cached_per_catalog(store: Store, tmp_path: Path) -> None:
    make_lesson(store, tmp_path)
    ask = fake(g=0.9)
    text = lessons.brief(store, task(), "W1", ask)
    assert "Уроки проекта" in text and "код принимает значения" in text and "Проверь: подай пустое" in text
    assert len(ask.calls) == 1
    assert lessons.brief(store, task(), "W2", ask) == text and len(ask.calls) == 1  # каталог тот же — без вызова
    make_lesson(store, tmp_path, "not-wired", tier="briefing")
    lessons.brief(store, task(), "W1", ask)
    assert len(ask.calls) == 2  # каталог сменился — пересчёт


def test_brief_hides_irrelevant_code_tier_and_triage(store: Store, tmp_path: Path) -> None:
    make_lesson(store, tmp_path)
    assert lessons.brief(store, task(), "W1", fake(g=0.2)) == ""
    make_lesson(store, tmp_path, "gate-weakened", tier="code")
    out = lessons.brief(store, task("TZ-10"), "W1", fake(g=0.9))
    assert "gate-weakened" not in out and "input-domain" not in out.replace("код принимает", "")
    assert lessons.brief(store, task(kind="triage"), "W1", fake()) == ""
    assert "в первую очередь" in lessons.brief(store, task("TZ-11", "review"), "W1", fake(g=0.9))


def test_brief_without_oracle_shows_recent_records_it_and_does_not_cache(store: Store, tmp_path: Path) -> None:
    make_lesson(store, tmp_path)
    assert "код принимает" in lessons.brief(store, task(), "W1", lambda *a, **k: None)
    with store.read() as tx:
        rows = [json.loads(r["payload"]) for r in tx.query("SELECT payload FROM events WHERE kind='lesson_shown'")]
    assert rows == [{"v": None, "lessons": ["input-domain"], "fallback": True}]
    oracle = fake(g=0.9)
    lessons.brief(store, task(), "W1", oracle)
    assert oracle.calls, "запасной показ не должен становиться кэшем"


def test_brief_never_raises(store: Store) -> None:
    def boom(*a, **k):
        raise RuntimeError("x")

    assert lessons.brief(store, None, "W1", boom) == ""
    assert lessons.brief(store, task(), "W1", boom) == ""


# ---- эффект ------------------------------------------------------------------------------------

def run_verdicts(store: Store, tmp_path: Path, shown: int, repeats: int) -> None:
    make_lesson(store, tmp_path)
    for i in range(shown):
        tz = f"TZ-{i}"
        add_task(store, tz)
        lessons.brief(store, task(tz), "W1", fake(g=0.9))
        if i < repeats:
            ev(store, "reject", tz, note=LONG)
        else:
            ev(store, "accept", tz, note="ok")
    lessons.scan(store, fake(choice="input-domain", p=0.95), limit=100)


def test_effect_verdicts(store: Store, tmp_path: Path) -> None:
    run_verdicts(store, tmp_path, shown=3, repeats=1)
    with store.read() as tx:
        assert lessons.effect(tx, lessons.catalog(tx)[0])["verdict"] == "мало данных"


def test_effect_ignores_verdicts_before_the_lesson_was_shown(store: Store, tmp_path: Path) -> None:
    make_lesson(store, tmp_path)
    for i in range(lessons.EXPOSED_MIN):
        tz = f"TZ-{i}"
        add_task(store, tz)
        ev(store, "accept", tz, note="ok")
        lessons.brief(store, task(tz), "W1", fake(g=0.9))
    with store.read() as tx:
        s = lessons.effect(tx, lessons.catalog(tx)[0])
    assert s["shown"] == lessons.EXPOSED_MIN and s["verdicts"] == 0 and s["verdict"] == "мало данных"


def test_effect_does_not_count_rejects_older_than_the_lesson(store: Store, tmp_path: Path) -> None:
    add_task(store, "TZ-1")
    ev(store, "reject", "TZ-1", note=LONG)
    make_lesson(store, tmp_path)
    lessons.scan(store, fake(choice="input-domain", p=0.95), limit=10)
    with store.read() as tx:
        s = lessons.effect(tx, lessons.catalog(tx)[0])
    assert s["unshown_repeats"] == 0 and s["repeats"] == 0


def test_effect_compares_only_inside_the_lesson_scope(store: Store, tmp_path: Path) -> None:
    make_lesson(store, tmp_path)
    with store.tx() as tx:
        tx.record_event(kind="accept", project="Q", tz="TZ-1", worker_id=None, payload={})
    with store.read() as tx:
        assert lessons.effect(tx, lessons.catalog(tx)[0])["unshown_verdicts"] == 0


def test_effect_works_when_defect_stops_recurring(store: Store, tmp_path: Path) -> None:
    run_verdicts(store, tmp_path, shown=10, repeats=0)
    with store.read() as tx:
        s = lessons.effect(tx, lessons.catalog(tx)[0])
    assert s["verdict"] == "работает" and s["shown"] == 10 and s["repeats"] == 0


def test_effect_fails_and_lesson_moves_out_of_graph_candidates(store: Store, tmp_path: Path) -> None:
    run_verdicts(store, tmp_path, shown=10, repeats=5)
    with store.read() as tx:
        s = lessons.effect(tx, lessons.catalog(tx)[0])
    assert s["verdict"] == "не работает" and s["repeats"] == 5
    assert any("НЕ ЛЕЧИТСЯ СТРОКОЙ" in line for line in lessons.report(store))
    assert lessons.candidates(store) == []


# ---- граф и снятие -----------------------------------------------------------------------------

def test_global_lessons_are_exported_in_memory_save_shape(store: Store, tmp_path: Path) -> None:
    make_lesson(store, tmp_path, "any-input", scope="global")
    make_lesson(store, tmp_path, "only-here")  # project/briefing — в граф не идёт
    out = lessons.candidates(store)
    assert [c["fleet_lesson"] for c in out] == ["P/any-input"]
    assert {"name", "behavior", "reason_incident", "trigger", "scope"} <= out[0].keys()
    target = tmp_path / "out" / "c.json"
    assert lessons.export_candidates(store, target) is True
    assert lessons.export_candidates(store, target) is False  # не менялось — не перезаписываем


def test_retire_removes_from_catalog_and_brief(store: Store, tmp_path: Path) -> None:
    make_lesson(store, tmp_path)
    assert lessons.retire(store, "P", "input-domain") is True
    assert lessons.retire(store, "P", "input-domain") is False
    assert lessons.brief(store, task(), "W1", fake(g=0.9)) == ""


def test_catalog_replays_add_retire_add_in_order(store: Store, tmp_path: Path) -> None:
    make_lesson(store, tmp_path)
    assert lessons.retire(store, "P", "input-domain") is True
    make_lesson(store, tmp_path)
    with store.read() as tx:
        assert [x["id"] for x in lessons.catalog(tx)] == ["input-domain"]


def test_attention_names_code_tier_and_failed_lessons_via_plain_connection(store: Store, tmp_path: Path) -> None:
    import sqlite3

    run_verdicts(store, tmp_path, shown=10, repeats=5)       # input-domain: не работает
    make_lesson(store, tmp_path, "gate-weakened", tier="code")
    conn = sqlite3.connect(store.path)
    conn.row_factory = sqlite3.Row
    out = lessons.attention(lessons.ConnTx(conn))
    conn.close()
    assert any("не работает: P/input-domain" in line for line in out)
    assert any("в код: P/gate-weakened" in line for line in out)


def test_parse_caps_text_length(tmp_path: Path) -> None:
    path = tmp_path / "x.md"
    path.write_text(LESSON_MD.replace("Проверяй вход", "Проверяй вход " + "и " * 300), encoding="utf-8")
    with pytest.raises(ValueError, match="rule: длиннее"):
        lessons.parse_lesson(path)


def test_tick_survives_failures(store: Store, monkeypatch) -> None:
    monkeypatch.setattr(lessons, "scan", lambda *a, **k: 1 / 0)
    out = lessons.tick(store)
    assert out["scanned"].startswith("error") and "mining" in out and "exported" in out
