from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet import outcome
from dev_mcp.fleet.store import Store
from dev_mcp.owner.actions import ActionRequest, OwnerActions, classify
from dev_mcp.owner.decision_book import SourceRef
from dev_mcp.owner.ticktick import PublishedTask, TickTickAdapter, VerifiedTask


class FakeClient:
    def __init__(self):
        self.creates = []
        self.fetches = []
        self.created_id = "task-100"
        self.url = "https://ticktick.com/webapp/#p/task-100"
        self.state = "active"
        self.completed_at = "2099-01-01T00:00:00+00:00"
        self.marker = None
        self.fails = False

    def create(self, payload, *, credential, idempotency_key):
        assert credential == "fake-for-test"
        self.creates.append((payload, idempotency_key))
        self.marker = "owner-action:" + idempotency_key
        if self.fails:
            raise TimeoutError("synthetic unreachable")
        return PublishedTask(self.created_id, self.url)

    def fetch(self, task_id, *, credential):
        assert credential == "fake-for-test"
        self.fetches.append(task_id)
        return VerifiedTask(task_id, self.marker, self.state, self.completed_at)


@pytest.fixture
def env(tmp_path):
    store = Store.open(str(tmp_path / "owner.sqlite"))
    parent = outcome.create_outcome(
        store, project="Fleet Platform", title="Verified intent", intent_id="intent-1",
        outcome_id="outcome-1", spec={"goal": "perform result"},
        evidence_spec={"kind": "external", "subject_version": "source-v3"},
        authority={"owner": False},
    )
    fake = FakeClient()
    adapter = TickTickAdapter(fake, lambda: "fake-for-test", operator_authorized=True)
    yield store, OwnerActions(store, adapter), fake, parent
    store.close()


def req(**kwargs):
    defaults = dict(
        outcome_id="outcome-1", subject_version="source-v3", cause="owner_physical",
        title="Confirm equipment setup", proposed_choice="Connect device X",
        reason="Physical access is required", evidence=(SourceRef("outcome", "outcome-1", "source-v3"),),
    )
    defaults.update(kwargs)
    return ActionRequest(**defaults)


def test_once_published_contains_proposal_cause_outcome_version_and_no_credentials(env):
    store, actions, fake, _ = env
    request = req()
    first = actions.publish(request)
    assert first.status == "published" and first.remote_id == "task-100"
    assert actions.publish(request) == first
    assert actions.route(request) == first
    assert actions.publish(replace(request, title="Reworded title",
                                   reason="Same request, later description")) == first
    assert len(fake.creates) == 1
    payload, key = fake.creates[0]
    assert payload["title"] == request.title
    for fragment in (request.proposed_choice, request.reason, "outcome-1", "source-v3",
                     "owner-action:" + first.action_id):
        assert fragment in payload["content"]
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM owner_action_requests") == 1
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='owner.action_published'") == 1
        assert not any("fake-for-test" in str(row["payload"]) for row in tx.query("SELECT payload FROM events"))


def test_verified_completion_is_exactly_once_versioned_evidence(env):
    store, actions, fake, _ = env
    published = actions.publish(req())
    fake.state = "completed"
    first = actions.complete(published.action_id, published.remote_id)
    assert first.status == "completed"
    assert actions.complete(published.action_id, published.remote_id) == first
    with store.read() as tx:
        row = tx.one("SELECT kind,ref,subject_version,verdict,producer FROM evidence")
        assert dict(row) == {
            "kind": "external", "ref": "owner-action:" + published.action_id,
            "subject_version": "source-v3", "verdict": "pass", "producer": "ticktick-verified",
        }
        assert tx.scalar("SELECT COUNT(*) FROM evidence") == 1
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='owner.action_resolution'") == 1
    assert fake.fetches == ["task-100"]


def test_reject_refusal_never_issues_success_evidence(env):
    store, actions, fake, _ = env
    first = actions.publish(req())
    fake.state = "refused"
    result = actions.complete(first.action_id, first.remote_id)
    assert result.status == "refused"
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM evidence") == 0
    assert actions.complete(first.action_id, first.remote_id) == result


def test_remote_refusal_without_completed_at_is_durable_without_success_evidence(env):
    store, actions, fake, _ = env
    first = actions.publish(req())
    fake.state = "refused"  # TickTick MCP maps cancelled/refused to refused.
    fake.completed_at = ""  # Neither refusal state guarantees completedAt.
    result = actions.complete(first.action_id, first.remote_id)
    assert result.status == "refused"
    assert actions.complete(first.action_id, first.remote_id) == result
    assert fake.fetches == [first.remote_id]
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM evidence") == 0
        event = tx.one("SELECT payload FROM events WHERE kind='owner.action_resolution'")
        assert event is not None
        import json
        payload = json.loads(event["payload"])
        assert payload["status"] == "refused"
        assert payload["time_source"] == "remote_status_observed_on_poll"
        assert tx.scalar("SELECT state FROM outcomes WHERE id='outcome-1'") == "open"


def test_refusal_with_explicit_stale_timestamp_is_rejected(env):
    _, actions, fake, _ = env
    first = actions.publish(req())
    fake.state = "refused"
    fake.completed_at = "2000-01-01T00:00:00+00:00"
    with pytest.raises(ValueError, match="predates"):
        actions.complete(first.action_id, first.remote_id)
    assert actions.get(first.action_id).status == "published"


def test_foreign_task_id_marker_and_early_completion_are_rejected(env):
    store, actions, fake, _ = env
    receipt = actions.publish(req())
    with pytest.raises(ValueError, match="foreign"):
        actions.complete(receipt.action_id, "different-task")
    fake.marker = "owner-action:foreign-record"
    fake.state = "completed"
    with pytest.raises(ValueError, match="does not match"):
        actions.complete(receipt.action_id, receipt.remote_id)
    fake.marker = "owner-action:" + receipt.action_id
    fake.completed_at = "2000-01-01T00:00:00+00:00"
    with pytest.raises(ValueError, match="predates"):
        actions.complete(receipt.action_id, receipt.remote_id)
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM evidence") == 0


def test_foreign_collision_prevents_association(env):
    store, actions, fake, _ = env
    first = actions.publish(req())
    with pytest.raises(ValueError, match="collision"):
        actions.publish(req(cause="owner_taste", title="Another decision"))
    assert actions.get(first.action_id).remote_id == "task-100"
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM owner_action_requests WHERE remote_id='task-100'") == 1


def test_timeout_is_uncertain_and_poll_never_reposts(env):
    _, actions, fake, _ = env
    fake.fails = True
    first = actions.publish(req())
    assert first.status == "uncertain"
    assert actions.publish(req()) == first
    assert actions.get(first.action_id) == first
    assert len(fake.creates) == 1


def test_capability_gap_creates_one_platform_child_not_owner_task(env):
    store, actions, fake, _ = env
    gap = req(cause="missing_tool", title="Need connector")
    assert classify(gap) == "capability_gap"
    first = actions.route(gap)
    assert isinstance(first, str) and first.startswith("capability-gap-")
    assert actions.route(gap) == first
    assert actions.route(replace(gap, proposed_choice="Different phrasing")) == first
    child = outcome.get_outcome(store, first)
    assert child.parent_id == "outcome-1" and child.spec["kind"] == "capability_gap"
    assert child.spec["subject_version"] == "source-v3"
    assert child.spec["evidence"][0]["version"] == "source-v3"
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM owner_action_requests") == 0
        assert tx.scalar("SELECT COUNT(*) FROM outcomes") == 2
    assert fake.creates == []


def test_scoped_reversible_default_has_deadline_rollback_no_ticktick(env):
    store, actions, fake, _ = env
    default_req = req(
        cause="owner_taste", reversible=True,
        deadline="2098-01-01T00:00:00+00:00", rollback="restore old layout",
        default_choice="Use old layout",
    )
    record = actions.route(default_req)
    assert classify(default_req) == "owner_only"
    assert record.status == "default_pending"
    assert actions.advance_default(record.action_id, now="2097-01-01T00:00:00+00:00") == record
    ready = actions.advance_default(record.action_id, now="2098-01-02T00:00:00+00:00")
    assert ready.status == "default_ready"
    assert actions.advance_default(record.action_id, now="2098-01-03T00:00:00+00:00") == ready
    assert fake.creates == []
    with store.read() as tx:
        event = tx.one("SELECT payload FROM events WHERE kind='owner.default_ready'")
        assert event
        import json
        command = json.loads(event["payload"])
        assert command["choice"] == "Use old layout"
        assert command["rollback"] == "restore old layout"
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='owner.default_ready'") == 1
        assert tx.scalar("SELECT COUNT(*) FROM evidence") == 0


def test_invalid_source_version_and_expired_default_rejected(env):
    _, actions, _, _ = env
    with pytest.raises(ValueError, match="stale"):
        actions.route(req(subject_version="old-v1"))
    with pytest.raises(ValueError, match="default requires"):
        req(default_choice="x")
    with pytest.raises(ValueError, match="expired"):
        req(reversible=True, rollback="undo", default_choice="go",
            deadline="2020-01-01T00:00:00+00:00")


def test_disabled_adapter_leaves_receipt_prepared_without_attempt(tmp_path):
    store = Store.open(str(tmp_path / "disabled.sqlite"))
    try:
        outcome.create_outcome(
            store, project="Fleet Platform", title="Real authorization required",
            intent_id="i1", outcome_id="outcome-1", spec={"goal": "owner decision"},
            evidence_spec={"subject_version": "source-v3"},
            authority={"owner": True},
        )
        client = FakeClient()
        disabled = OwnerActions(
            store, TickTickAdapter(client, lambda: "fake-for-test"))
        pending = disabled.publish(req())
        assert pending.status == "prepared"
        assert client.creates == []
        with store.read() as tx:
            assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='owner.delivery_started'") == 0

        enabled = OwnerActions(
            store, TickTickAdapter(client, lambda: "fake-for-test",
                                   operator_authorized=True))
        delivered = enabled.publish(req())
        assert delivered.action_id == pending.action_id and delivered.status == "published"
        assert len(client.creates) == 1
    finally:
        store.close()


def test_missing_operator_credentials_does_not_poison_receipt(env):
    store, _, fake, _ = env
    disabled = OwnerActions(
        store, TickTickAdapter(fake, lambda: "", operator_authorized=True))
    assert disabled.publish(req()).status == "prepared"
    with store.read() as tx:
        assert tx.scalar("SELECT COUNT(*) FROM events WHERE kind='owner.delivery_started'") == 0
