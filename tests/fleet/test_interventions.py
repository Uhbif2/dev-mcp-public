"""TZ-48: manual evidence is captured once and leads to scoped remediation."""
from __future__ import annotations

import pytest

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from dev_mcp.fleet.interventions import (
    Intervention, InterventionLedger, audit_interventions, classify,
    git_interventions, guard_approvals, guard_events, integrator_commits,
    lesson_interventions,
)
from dev_mcp.fleet.store import Store
from dev_mcp.fleet.spec import parse_spec
from dev_mcp.fleet import flow as flow_metrics

AT = "2026-10-11T05:00:00+00:00"


def setup(tmp_path: Path):
    root = tmp_path / "platform"
    root.mkdir()
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root) VALUES(?,?)",
                  ("Fleet Platform", str(root)), event="seed")
    return store, root, InterventionLedger(store, "Fleet Platform", root)


def events(store: Store, kind: str):
    with store.read() as tx:
        return [dict(r) for r in tx.query("SELECT * FROM events WHERE kind=?", (kind,))]


def test_git_excludes_proven_integrator_not_merely_bot_author(tmp_path):
    accepted, manual = "a" * 40, "b" * 40
    store, root, ledger = setup(tmp_path)
    with store.tx() as tx:
        tx.record_event(kind="integrate", payload={"commit": accepted})
    assert integrator_commits(store) == {accepted}

    def runner(argv, **kwargs):
        assert "--no-merges" in argv
        assert "--no-show-signature" in argv
        output = (
            f"{accepted}\x1foperator\x1f{AT}\x1fnot manual\x1e"
            f"{manual}\x1ffleet-bot\x1f{AT}\x1fsecret=supersecret\x1e"
        )
        return SimpleNamespace(returncode=0, stdout=output)

    got = git_interventions(root, integrator_shas=integrator_commits(store),
                            refs=["platform/work", "master"], runner=runner)
    assert len(got) == 1
    assert got[0].identifier == manual
    assert got[0].actor == "fleet-bot"
    assert ledger.ingest(got[0])
    assert not ledger.ingest(got[0])
    assert len(events(store, "intervention")) == 1
    draft = next((root / "ToDo/tz-drafts").glob("TZ-*.md"))
    assert "supersecret" not in draft.read_text(encoding="utf-8")
    assert manual not in draft.read_text(encoding="utf-8")


def test_actual_short_integrator_sha_is_not_counted_as_manual(tmp_path):
    """Integrator._commit emits rev-parse --short, not a 40-character hash."""
    integrated = "c" * 40
    manual = "d" * 40
    store, root, ledger = setup(tmp_path)
    with store.tx() as tx:
        tx.record_event(kind="integrate", payload={"commit": integrated[:7]})
    assert integrator_commits(store) == {integrated[:7]}

    def runner(argv, **_kwargs):
        return SimpleNamespace(returncode=0, stdout=(
            f"{integrated}\x1ffleet\x1f{AT}\x1fintegrated\x1e"
            f"{manual}\x1foperator\x1f{AT}\x1fmanual\x1e"
        ))

    found = git_interventions(root, integrator_shas=integrator_commits(store),
                              refs=["platform/work"], runner=runner)
    assert [entry.identifier for entry in found] == [manual]
    assert ledger.tick(repositories=[root], runner=runner)["new"] == 1
    assert len(events(store, "intervention")) == 1


def test_repeated_class_does_not_create_second_draft(tmp_path):
    store, root, ledger = setup(tmp_path)
    for i in range(4):
        assert ledger.ingest(Intervention("fleet-cli", str(i), "recover",
                                          "operator", AT, f"audit:{i}"))
    assert sorted((root / "ToDo/tz-drafts").glob("TZ-*.md")).__len__() == 1
    assert len(events(store, "intervention")) == 4
    assert len(events(store, "intervention.draft")) == 1
    assert not ledger.ingest(Intervention("fleet-cli", "1", "recover",
                                          "operator", AT, "audit:1"))
    row = events(store, "intervention")[0]
    payload = json.loads(row["payload"])
    assert payload["actor"] == "operator"
    assert payload["action"] == "recover"
    assert payload["occurred_at"] == AT
    assert payload["reference"] == "audit:0"
    # Reconstructing the ledger does not generate schema events every tick.
    old_schema_events = len(events(store, "intervention.schema"))
    InterventionLedger(store, "Fleet Platform", root)
    assert len(events(store, "intervention.schema")) == old_schema_events
    assert parse_spec(str(next((root / "ToDo/tz-drafts").glob("TZ-*.md")))).tz.startswith("TZ-")


def test_accepted_remedy_repeated_class_creates_one_defect(tmp_path):
    store, root, ledger = setup(tmp_path)
    with store.tx() as tx:
        tx.change("""INSERT INTO tasks (project,tz,state,created_at,updated_at)
                     VALUES ('Fleet Platform','TZ-44','accepted',?,?)""",
                  ("2026-10-10T00:00:00+00:00", "2026-10-10T01:00:00+00:00"),
                  event="seed")
        tx.record_event(kind="accept", project="Fleet Platform", tz="TZ-44",
                        ts="2026-10-10T01:00:00+00:00")
    historic = Intervention("guard-install", "old", "install", "operator",
                            "2026-10-09T00:00:00+00:00", "guard-log:old", "TZ-44")
    recent = Intervention("guard-install", "new", "install", "operator",
                          AT, "guard-log:new", "TZ-44")
    assert ledger.ingest(historic)
    assert not list((root / "ToDo/tz-drafts").glob("TZ-*.md"))
    assert ledger.ingest(recent)
    assert ledger.ingest(Intervention("guard-install", "new2", "install",
                                      "operator", AT, "guard-log:new2", "TZ-44"))
    files = list((root / "ToDo/tz-drafts").glob("TZ-*.md"))
    assert len(files) == 1
    assert "Дефект устранения" in files[0].read_text(encoding="utf-8")
    assert len(events(store, "intervention.draft")) == 1


def test_rotated_audit_cli_guard_and_no_agent_events(tmp_path):
    path = tmp_path / "audit.jsonl"
    entries = [
        {"ts": AT, "tool": "run", "decision": "allow", "agent": "",
         "args": {"argv": ["fleet.cmd", "reactivate", "private-secret"]}},
        {"ts": AT, "tool": "run", "decision": "allow", "agent": "W7",
         "args": {"argv": ["fleet.cmd", "reactivate"]}},
        {"ts": AT, "tool": "run", "decision": "allow", "agent": "",
         "args": {"argv": ["guard.cmd", "install"]}},
        {"ts": AT, "tool": "run", "decision": "deny", "agent": "",
         "args": {"argv": ["fleet.cmd", "freeze"]}},
    ]
    path.write_text("\n".join(json.dumps(x) for x in entries) + "\n", encoding="utf-8")
    got = audit_interventions(path)
    assert [(x.source, x.action) for x in got] == [
        ("fleet-cli", "reactivate"), ("guard-install", "install")]
    assert "private-secret" not in repr(got)


def test_audit_identity_is_stable_across_since_windows_and_archive_rotation(tmp_path):
    path = tmp_path / "audit.jsonl"
    early = {"ts": "2026-10-10T01:00:00+00:00", "tool": "run", "decision": "allow",
             "agent": "", "args": {"argv": ["fleet.cmd", "freeze", "not-a-secret"]}}
    later = {"ts": AT, "tool": "run", "decision": "allow",
             "agent": "", "args": {"argv": ["fleet.cmd", "reactivate", "SENSITIVE"]}}
    path.write_text(json.dumps(early) + "\n" + json.dumps(later) + "\n", encoding="utf-8")
    baseline = audit_interventions(path)[1]
    assert audit_interventions(path, since="2026-10-11")[0].identifier == baseline.identifier
    (tmp_path / "audit-2026-10-10.jsonl").write_text(
        json.dumps(early) + "\n", encoding="utf-8")
    path.write_text(json.dumps(later) + "\n", encoding="utf-8")
    assert audit_interventions(path)[1].identifier == baseline.identifier
    assert "SENSITIVE" not in repr(baseline)


def test_deduplicates_sanitized_long_identifiers(tmp_path):
    store, root, ledger = setup(tmp_path)
    original = Intervention("owner-action", "x" * 210, "owner.action_override",
                            "owner", AT, "fleet-event:100")
    assert ledger.ingest(original)
    assert not ledger.ingest(original)
    assert len(events(store, "intervention")) == 1


def test_missing_linked_remediation_generates_one_draft(tmp_path):
    store, root, ledger = setup(tmp_path)
    case = Intervention("lesson", "123", "lesson-unknown-predicate", "bootstrap",
                        "2026-10-10T00:00:00+00:00", "docs/lessons.md#case-123",
                        "TZ-404")
    assert ledger.ingest(case)
    assert not ledger.ingest(case)
    candidates = list((root / "ToDo/tz-drafts").glob("TZ-*.md"))
    assert len(candidates) == 1
    assert parse_spec(str(candidates[0])).tz.startswith("TZ-")
    assert "docs/lessons.md#case-123" in json.dumps(
        [json.loads(r["payload"]) for r in events(store, "intervention")])


def test_approvals_guard_logs_owner_events_and_metrics(tmp_path):
    store, root, ledger = setup(tmp_path)
    approvals = tmp_path / "approved.txt"
    approvals.write_text("a" * 40 + " operator secret\n", encoding="utf-8")
    # Урок 48: время одобрения — mtime файла; без фиксации тест краснел после полуночи UTC.
    stamp = datetime.fromisoformat(AT).timestamp()
    os.utime(approvals, (stamp, stamp))
    assert len(guard_approvals(approvals)) == 1
    log = tmp_path / "guard.jsonl"
    log.write_text(json.dumps({"ts": AT, "action": "install", "origin": "operator",
                               "secret": "DO_NOT_STORE"}) + "\n", encoding="utf-8")
    assert len(guard_events(log)) == 1
    with store.tx() as tx:
        tx.record_event(kind="owner.action_manual", ts=AT, payload={"secret": "DO_NOT_STORE"})
    output = ledger.tick(approvals_path=approvals, guard_log=log)
    assert output["new"] == 3
    assert len(events(store, "intervention")) == 3
    assert "DO_NOT_STORE" not in str([dict(r) for r in
                                      store._connection().execute("SELECT * FROM intervention_log")])
    series = ledger.metrics(now=datetime(2026, 10, 11, 9, tzinfo=timezone.utc), days=3)
    assert series["goal"] == 0
    assert series["today"] == 3
    assert sum(series["by_class"].values()) == 3
    daily = flow_metrics.intervention_counts(
        store._connection(), now=datetime(2026, 10, 11, 9, tzinfo=timezone.utc), days=3)
    assert daily["today"] == 3 and sum(daily["by_class"].values()) == 3
    rendered = flow_metrics.format_flow(flow_metrics.flow(store._connection(), [], AT))
    assert any("ручных вмешательств" in line for line in rendered)


def test_flow_is_usable_before_intervention_schema_exists():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    try:
        data = flow_metrics.intervention_counts(conn, now=datetime(2026, 10, 11, tzinfo=timezone.utc))
        assert data["today"] == 0 and data["by_class"] == {}
    finally:
        conn.close()


def test_owner_console_mutation_emits_durable_metadata_only_receipt(tmp_path):
    from tools import fleet as owner_cli
    store, _root, ledger = setup(tmp_path)
    code, message = owner_cli._mutate(
        Path(store.path), lambda _conn: "result contains private-secret",
        intervention_command="freeze")
    assert code == 0 and "private-secret" in message
    with store.read() as tx:
        rows = tx.query("SELECT payload FROM events WHERE kind='owner_cli_manual'")
    assert len(rows) == 1
    assert json.loads(rows[0]["payload"]) == {"actor": "console", "command": "freeze"}
    assert ledger.tick()["new"] == 1
    assert ledger.tick()["new"] == 0
    recorded = json.loads(events(store, "intervention")[0]["payload"])
    assert recorded["class"] == "manual-freeze"
    assert "private-secret" not in str(recorded)


def test_failed_owner_console_command_rolls_back_receipt_and_mutation(tmp_path):
    import pytest
    from tools import fleet as owner_cli

    store, root, ledger = setup(tmp_path)
    def fails_after_sql(conn):
        conn.execute("UPDATE projects SET root='should-not-stick' WHERE name='Fleet Platform'")
        raise ValueError("private-failure-detail")

    with pytest.raises(ValueError, match="private-failure-detail"):
        owner_cli._mutate(Path(store.path), fails_after_sql,
                          intervention_command="reactivate")
    with store.read() as tx:
        assert tx.scalar("SELECT root FROM projects WHERE name='Fleet Platform'") == str(root)
        assert tx.one("SELECT id FROM events WHERE kind='owner_cli_manual'") is None
    assert ledger.tick()["new"] == 0


def test_lessons_are_seeded_without_copying_prose(tmp_path):
    store, root, ledger = setup(tmp_path)
    file = root / "lessons.md"
    file.write_text("# Incidents\n| # | Case | Class | Invariant | Where |\n"
                    "| 1 | secret=NEVER_INCLUDE | Класс один | test | TZ-50 |\n"
                    "| 2 | another secret | Класс два | test | TZ-44 |\n", encoding="utf-8")
    source = lesson_interventions(file)
    assert len(source) == 2
    assert source[0].resolution == "TZ-50"
    result = ledger.tick(lessons_path=file)
    assert result["new"] == 2
    assert ledger.tick(lessons_path=file)["new"] == 0
    assert "NEVER_INCLUDE" not in str(events(store, "intervention"))
    assert classify(source[0]).startswith("lesson-")


@pytest.mark.production
def test_actual_russian_lessons_remain_distinct_failure_classes():
    source = Path(__file__).resolve().parents[2] / "docs" / "LESSONS-AUTONOMY-2026-10.md"
    items = lesson_interventions(source)
    assert len(items) >= 25
    # UTF-8 failure classes cannot all collapse to the same `lesson-other`.
    assert len({classify(item) for item in items}) >= 15
    assert all("lesson-other" != classify(item) for item in items[:20])


@pytest.mark.production
def test_seed_full_lessons_once_and_keep_drafts_machine_readable(tmp_path):
    store, root, ledger = setup(tmp_path)
    source = Path(__file__).resolve().parents[2] / "docs" / "LESSONS-AUTONOMY-2026-10.md"
    result = ledger.tick(lessons_path=source)
    assert result["new"] == len(lesson_interventions(source))
    assert result["new"] >= 25
    assert ledger.tick(lessons_path=source)["new"] == 0
    candidates = list((root / "ToDo/tz-drafts").glob("TZ-*.md"))
    assert len(candidates) >= 15
    assert all(parse_spec(str(path)).tz.startswith("TZ-") for path in candidates)
