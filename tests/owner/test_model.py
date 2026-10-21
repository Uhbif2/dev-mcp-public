from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.owner.decision_book import Question, Scope, SourceRef, resolve, DecisionBook  # noqa: E402
from dev_mcp.owner.model import OwnerModel, Preference  # noqa: E402
from dev_mcp.fleet.store import Store  # noqa: E402

FUTURE = "2099-01-01T00:00:00+00:00"
NOW = "2026-10-09T12:00:00+00:00"


def preference(*, key="font", choice="serif", root="C:/Work/A",
               ref="reply:1", version="v1", confidence=0.95,
               private=False, project="P"):
    return Preference(key, choice, Scope(root, project),
                      SourceRef("owner_reply", ref, version),
                      confidence, FUTURE, private)


def test_private_content_not_collected_or_logged(tmp_path):
    model = OwnerModel()
    private = preference(choice="confidential description of owner", private=True)
    assert not model.observe(private)
    assert "confidential" not in repr(model._observations)
    assert "confidential" not in repr(private)
    assert model.propose(key="font", scope=Scope("C:/Work/A", "P"),
                         choices=("serif",), now=NOW) is None


def test_single_versioned_preference_and_revoke():
    model = OwnerModel()
    item = preference()
    assert model.observe(item)
    proposal = model.propose(key="font", scope=item.scope,
                             choices=("serif", "sans"), now=NOW)
    assert proposal.choice == "serif"
    assert proposal.source == SourceRef("previous_choice", "reply:1", "v1")
    assert model.observe(item)
    assert len(model._observations) == 1
    model.revoke(item.source)
    assert model.propose(key="font", scope=item.scope, choices=("serif",), now=NOW) is None


def test_conflict_in_same_scope_not_drowned_by_high_confidence():
    model = OwnerModel()
    model.observe(preference())
    model.observe(preference(choice="sans", ref="reply:2", confidence=0.99))
    assert model.propose(key="font", scope=Scope("C:/Work/A", "P"),
                         choices=("serif", "sans"), now=NOW) is None


def test_cross_root_and_scoped_preference_disambiguation():
    model = OwnerModel()
    model.observe(preference(root="C:/Work/A"))
    model.observe(preference(root="D:/Work/A", choice="sans", ref="reply:3"))
    assert model.propose(key="font", scope=Scope("D:/Work/A", "P"),
                         choices=("serif", "sans"), now=NOW).choice == "sans"
    assert model.propose(key="font", scope=Scope("D:/Work/A", "Other"),
                         choices=("serif", "sans"), now=NOW) is None


def test_fake_confidence_and_versioned_choice():
    model = OwnerModel()
    with pytest.raises(ValueError, match="confidence"):
        model.observe(preference(confidence=1.1))
    model.observe(preference(confidence=0.4))
    assert model.propose(key="font", scope=Scope("C:/Work/A", "P"),
                         choices=("serif",), now=NOW) is None


def test_model_can_feed_last_priority_without_raw_notes(tmp_path):
    model = OwnerModel()
    model.observe(preference())
    store = Store.open(str(tmp_path / "decisions.sqlite"))
    try:
        book = DecisionBook(store)
        q = Question("font", Scope("C:/Work/A", "P"),
                     ("serif", "sans"), reversible=True, rollback="restore font")
        result = resolve(
            book, q, broker=lambda _: [],
            previous_choices=lambda _: [model.propose(key=q.key, scope=q.scope,
                                                       choices=q.choices, now=NOW)],
            verify_version=lambda _: True, now=NOW,
        )
        assert result.choice == "serif" and result.cause == "previous"
        assert book.get(result.decision_id).source.version == "v1"
    finally:
        store.close()


def test_persist_nonprivate_and_exclude_private_with_reopen(tmp_path):
    store = Store.open(str(tmp_path / "prefs.sqlite"))
    try:
        model = OwnerModel(store)
        model.observe(preference(choice="serif"))
        secret = preference(ref="private:2", choice="my secret", private=True)
        assert not model.observe(secret)
        assert not model.observe(Preference(
            "font", "another secret", Scope("C:/Work/A", "P"),
            SourceRef("owner_reply", "private:3", "v1"), 0.99, FUTURE,
        ))  # safe default is private
        again = OwnerModel(store)
        assert again.propose(key="font", scope=Scope("C:/Work/A", "P"),
                             choices=("serif",), now=NOW).choice == "serif"
        with store.read() as tx:
            rows = tx.query("SELECT * FROM owner_preferences")
            assert len(rows) == 1
            assert "my secret" not in str(rows)
        again.revoke(preference().source)
        assert OwnerModel(store).propose(key="font", scope=Scope("C:/Work/A", "P"),
                                         choices=("serif",), now=NOW) is None
    finally:
        store.close()
