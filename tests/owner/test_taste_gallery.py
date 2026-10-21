from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet import outcome
from dev_mcp.fleet.store import Store
from dev_mcp.owner.decision_book import DecisionBook
from dev_mcp.owner.taste_gallery import (
    TasteError, TasteOption, choose_taste, gallery_snapshot, publish_gallery,
)


def test_dashboard_token_never_grants_global_owner():
    """Bite without FastMCP: execute the real dashboard auth handlers in isolation.

    Must fail on the rejected version where login calls owner.unlock(token).
    """
    import ast
    import json
    import re
    import secrets
    import threading
    import time
    from types import SimpleNamespace

    source = Path(__file__).resolve().parents[2] / "src/dev_mcp/server.py"
    module = ast.parse(source.read_text(encoding="utf-8"))
    server = next(n for n in module.body if isinstance(n, ast.FunctionDef)
                  and n.name == "build_server")
    names = {"_dashboard_denied", "_dashboard_owner", "owner_dashboard_login"}
    handlers = [n for n in server.body if isinstance(n, ast.FunctionDef)
                and n.name in names]
    assert {n.name for n in handlers} == names
    for handler in handlers:
        handler.decorator_list = []  # MCP wrapper omitted; actual handler body runs.

    class FakeOwner:
        ttl_s = 7200

        def __init__(self):
            self.grants = set()

        def unlock(self, identity, password):
            if password != "correct-test-password":
                return False, "denied"
            self.grants.add(identity)
            return True, "ok"

        def lock(self, identity):
            self.grants.discard(identity)

        def is_owner(self, identity):
            return identity in self.grants

    owner = FakeOwner()
    fleet_ctx = SimpleNamespace(owner=owner, is_owner=owner.is_owner)
    env = {
        "json": json, "re": re, "secrets": secrets, "threading": threading,
        "time": time, "fleet_ctx": fleet_ctx, "dashboard_sessions": {},
        "dashboard_lock": threading.Lock(), "dashboard_ttl_s": 3600.0,
    }
    ast_module = ast.fix_missing_locations(ast.Module(body=handlers, type_ignores=[]))
    exec(compile(ast_module, str(source), "exec"), env)
    result = json.loads(env["owner_dashboard_login"]("correct-test-password"))
    token = result["session_token"]
    assert env["_dashboard_owner"](token)  # normal dashboard calls still work
    assert not fleet_ctx.is_owner(token)  # fleet_owner uses THIS global guard
    assert not owner.grants  # one-use password-check identity was revoked


@pytest.fixture()
def store(tmp_path):
    s = Store.open(str(tmp_path / "fleet.sqlite"))
    with s.tx() as tx:
        tx.change("INSERT INTO projects(name,root) VALUES(?,?)",
                  ("P", "C:/P"), event="test.project")
    yield s
    s.close()


def setup_gallery(store, *, subject="v1", cause="owner_taste", proofs=True):
    obj = outcome.create_outcome(
        store, project="P", title="Screen taste", intent_id="intent-1",
        spec={"dashboard_visibility": "public"},
        evidence_spec={"subject_version": subject},
        authority={"policy": "scope-only"},
    )
    outcome.open_obstacle(
        store, outcome_id=obj.id, type="owner_action", cause_code=cause,
        predicate={}, owner_action="Private owner-only question",
    )
    ids = []
    for n in range(2):
        ids.append(outcome.add_evidence(
            store, writer=outcome.server_evidence_writer(store),
            outcome_id=obj.id, kind="screen" if proofs else "run",
            ref=f"SECRET-SCREEN-FILE-{n}", subject_version=subject,
            env_version="win:snapshot1", verdict="pass", producer="verified-screenshot",
        ))
    return obj, (TasteOption("style-one", "<strong>First</strong>", ids[0]),
                 TasteOption("style-two", "Second", ids[1]))


def test_empty_gallery_and_missing_proof_fail_closed(store):
    assert gallery_snapshot(store, "unknown")["status"] == "empty"
    obj, options = setup_gallery(store, proofs=False)
    with pytest.raises(TasteError, match="proof"):
        publish_gallery(store, outcome_id=obj.id, options=options,
                        expected_subject_version="v1")
    assert gallery_snapshot(store, obj.id)["status"] == "empty"


def test_gallery_requires_owner_taste_and_current_subject(store):
    obj, options = setup_gallery(store, cause="owner_physical")
    with pytest.raises(TasteError, match="owner_taste"):
        publish_gallery(store, outcome_id=obj.id, options=options,
                        expected_subject_version="v1")
    obj2, options2 = setup_gallery(store)
    with pytest.raises(TasteError, match="stale"):
        publish_gallery(store, outcome_id=obj2.id, options=options2,
                        expected_subject_version="v0")
    with pytest.raises(TasteError, match="different image"):
        publish_gallery(store, outcome_id=obj2.id,
                        options=(options2[0], TasteOption("duplicate-image", "Second",
                                                        options2[0].image_evidence_id)),
                        expected_subject_version="v1")


def test_gallery_versioned_options_and_idempotent_publish(store):
    obj, options = setup_gallery(store)
    revision = publish_gallery(store, outcome_id=obj.id, options=options,
                               expected_subject_version="v1")
    assert revision == 1
    assert publish_gallery(store, outcome_id=obj.id, options=options,
                           expected_subject_version="v1") == revision
    card = gallery_snapshot(store, obj.id)
    assert card["status"] == "ready"
    assert card["subject_version"] == "v1"
    assert card["revision"] == 1
    assert len(card["options"]) == 2
    assert "<strong>First</strong>" in card["options"][0]["label"]
    assert "SECRET-SCREEN-FILE" not in str(card)
    assert "Private owner-only question" not in str(card)


def test_authenticated_choice_records_decision_atomically(store):
    obj, options = setup_gallery(store)
    publish_gallery(store, outcome_id=obj.id, options=options,
                    expected_subject_version="v1")
    with pytest.raises(PermissionError):
        choose_taste(store, outcome_id=obj.id, option_id="style-one",
                     expected_revision=1)
    result = choose_taste(store, outcome_id=obj.id, option_id="style-one",
                          expected_revision=1, owner_authorized=True)
    assert result["status"] == "chosen"
    assert result["revision"] == 2
    book = DecisionBook(store)
    decision = book.get(result["decision_id"])
    assert decision.choice == "style-one"
    assert decision.source.kind == "owner_reply"
    assert decision.evidence[0].ref == options[0].image_evidence_id
    assert decision.evidence[0].version == "v1"
    assert gallery_snapshot(store, obj.id)["status"] == "chosen"
    with pytest.raises(TasteError, match="already"):
        choose_taste(store, outcome_id=obj.id, option_id="style-two",
                     expected_revision=1, owner_authorized=True)


def test_simultaneous_choices_yield_one_decision(store):
    obj, options = setup_gallery(store)
    publish_gallery(store, outcome_id=obj.id, options=options,
                    expected_subject_version="v1")
    def pick(option):
        try:
            return choose_taste(store, outcome_id=obj.id, option_id=option,
                                expected_revision=1, owner_authorized=True)["status"]
        except TasteError:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(pick, ("style-one", "style-two")))
    assert sorted(results) == ["chosen", "conflict"]
    with store.read() as tx:
        assert tx.scalar(
            "SELECT COUNT(*) FROM decision_provenance WHERE decision_key=?",
            ("taste:" + obj.id,),
        ) == 1


def test_stale_result_and_revoked_visual_proof(store):
    obj, options = setup_gallery(store)
    publish_gallery(store, outcome_id=obj.id, options=options,
                    expected_subject_version="v1")
    with store.tx() as tx:
        tx.change("UPDATE evidence SET verdict='fail' WHERE id=?",
                  (options[0].image_evidence_id,), event="test.revoke_proof")
    assert gallery_snapshot(store, obj.id)["reason"] == "image_proof_missing"
    with pytest.raises(TasteError, match="proof"):
        choose_taste(store, outcome_id=obj.id, option_id="style-one",
                     expected_revision=1, owner_authorized=True)
    with store.tx() as tx:
        tx.change("UPDATE outcomes SET evidence_spec=? WHERE id=?",
                  ('{"subject_version":"v2"}', obj.id),
                  event="test.change_version")
    assert gallery_snapshot(store, obj.id)["reason"] == "stale_gallery"
    with pytest.raises(TasteError, match="stale"):
        choose_taste(store, outcome_id=obj.id, option_id="style-two",
                     expected_revision=1, owner_authorized=True)


def test_partial_or_closed_taste_request_refuses_selection(store):
    obj, options = setup_gallery(store)
    publish_gallery(store, outcome_id=obj.id, options=options,
                    expected_subject_version="v1")
    with store.tx() as tx:
        tx.change("UPDATE obstacles SET state='resolved' WHERE outcome_id=?",
                  (obj.id,), event="test.close")
    assert gallery_snapshot(store, obj.id)["reason"] == "taste_request_closed"
    with pytest.raises(TasteError, match="owner_taste"):
        choose_taste(store, outcome_id=obj.id, option_id="style-one",
                     expected_revision=1, owner_authorized=True)


def test_new_version_may_offer_new_visuals_without_overwriting_previous_decision(store):
    obj, options = setup_gallery(store)
    publish_gallery(store, outcome_id=obj.id, options=options,
                    expected_subject_version="v1")
    first = choose_taste(store, outcome_id=obj.id, option_id="style-one",
                         expected_revision=1, owner_authorized=True)
    with store.tx() as tx:
        tx.change("UPDATE outcomes SET evidence_spec=? WHERE id=?",
                  ('{"subject_version":"v2"}', obj.id), event="test.version")
    fresh = tuple(TasteOption(
        f"fresh-{index}", f"Fresh style {index}",
        outcome.add_evidence(
            store, writer=outcome.server_evidence_writer(store),
            outcome_id=obj.id, kind="screen", ref=f"SECRET-V2-{index}",
            subject_version="v2", env_version="env-v2",
            verdict="pass", producer="screen-verifier",
        )
    ) for index in (1, 2))
    assert publish_gallery(store, outcome_id=obj.id, options=fresh,
                           expected_subject_version="v2") == 3
    second = choose_taste(store, outcome_id=obj.id, option_id="fresh-2",
                          expected_revision=3, owner_authorized=True)
    assert first["decision_id"] != second["decision_id"]
    assert DecisionBook(store).get(first["decision_id"]).choice == "style-one"
    assert DecisionBook(store).get(second["decision_id"]).choice == "fresh-2"


def test_project_names_with_spaces_keep_narrow_decision_scope(store):
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root) VALUES(?,?)",
                  ("Fleet Platform", "C:/Fleet"), event="test.project")
    obj = outcome.create_outcome(
        store, project="Fleet Platform", title="Illustration", intent_id="intent:style",
        spec={"dashboard_visibility": "public"},
        evidence_spec={"subject_version": "v1"}, authority={"access": "owner"},
    )
    outcome.open_obstacle(
        store, outcome_id=obj.id, type="owner_action", cause_code="owner_taste",
        predicate={}, owner_action="Choose preferred visual",
    )
    choices = [
        TasteOption(f"style-{i}", f"Style {i}", outcome.add_evidence(
            store, writer=outcome.server_evidence_writer(store),
            outcome_id=obj.id, kind="screen", ref=f"opaque-screenshot-{i}",
            subject_version="v1", env_version="env-v1", verdict="pass",
            producer="trusted-render",
        )) for i in (1, 2)
    ]
    publish_gallery(store, outcome_id=obj.id, options=choices,
                    expected_subject_version="v1")
    receipt = choose_taste(store, outcome_id=obj.id, option_id="style-1",
                           expected_revision=1, owner_authorized=True)
    saved = DecisionBook(store).get(receipt["decision_id"])
    assert saved.scope.root == "c:\\fleet"
    assert saved.scope.project.startswith("fleet-")
    assert saved.scope.project != "*"


@pytest.mark.asyncio
async def test_actual_mcp_dashboard_tools_reject_ungranted_and_serve_verified_image(
    store, tmp_path, monkeypatch,
):
    import base64
    import json
    from fastmcp import Client
    from dev_mcp import server
    from dev_mcp.audit import Audit
    from dev_mcp.fleet.owner import OwnerSessions
    from dev_mcp.policy import Policy

    image_root = tmp_path / "screens"
    image_root.mkdir()
    # In this test the server's project root is the source of allowed evidence.
    with store.tx() as tx:
        tx.change("UPDATE projects SET root=? WHERE name='P'",
                  (str(image_root),), event="test.root")
    examples = []
    for index in (1, 2):
        image = image_root / f"proof-{index}.png"
        image.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
            "AAAADUlEQVQIHWP4z8DwHwAFgAI/ScLttAAAAABJRU5ErkJggg=="
        ))
        examples.append(image)
    obj = outcome.create_outcome(
        store, project="P", title="Public title", intent_id="i:1",
        spec={"dashboard_visibility": "public"},
        evidence_spec={"subject_version": "v1"}, authority={"access": "owner"},
    )
    outcome.open_obstacle(
        store, outcome_id=obj.id, type="owner_action", cause_code="owner_taste",
        predicate={}, owner_action="Private taste question",
    )
    choices = []
    for idx, image in enumerate(examples):
        eid = outcome.add_evidence(
            store, writer=outcome.server_evidence_writer(store),
            outcome_id=obj.id, kind="screen", ref=str(image),
            subject_version="v1", env_version="test-env", verdict="pass",
            producer="trusted-image-verifier",
        )
        choices.append(TasteOption(f"style-{idx}", f"Variant {idx}", eid))
    publish_gallery(store, outcome_id=obj.id, options=choices,
                    expected_subject_version="v1")
    # A public owner-granted worker ID must NEVER authorize an MCP caller.
    session_ids = set()
    def owner_login(self, worker_id, password):
        if password != "test-password":
            return False, "denied"
        session_ids.add(worker_id)
        return True, "ok"
    monkeypatch.setattr(OwnerSessions, "unlock", owner_login)
    monkeypatch.setattr(OwnerSessions, "is_owner",
                        lambda self, agent: agent == "W9999" or agent in session_ids)
    policy_file = tmp_path / "policy.toml"
    policy_file.write_text(
        f"roots = ['{image_root.as_posix()}']\nallow_write = false\n",
        encoding="utf-8",
    )
    app = server.build_server(
        Policy.load(policy_file), Audit(tmp_path / "audit.jsonl"),
        fleet_db=Path(store.path),
    )

    async def invoke(client, name, args):
        response = await client.call_tool(name, args)
        return json.loads(response.content[0].text)

    async with Client(app) as client:
        # Bite: this exact forged legacy call succeeded before the fix.
        # A dashboard now rejects it, even while W9999 has an owner grant.
        try:
            legacy_response = await client.call_tool(
                "owner_dashboard_overview", {"agent": "W9999"}
            )
        except Exception:
            pass  # Missing server-minted token: rejected at tool boundary.
        else:
            legacy_body = json.loads(legacy_response.content[0].text)
            assert legacy_body.get("status") == "error"
        denied = await invoke(client, "owner_dashboard_overview", {"session_token": "W123"})
        assert denied == {"status": "error", "reason": "owner_authorization_required"}
        # Reproduces the former worker-ID spoofing flaw. The owner grant is
        # present but the client cannot impersonate its session.
        forged = await invoke(client, "owner_dashboard_overview", {"session_token": "W9999"})
        assert forged["reason"] == "owner_authorization_required"
        denied_image = await invoke(client, "owner_dashboard_image", {
            "outcome_id": obj.id, "image_proof_id": choices[0].image_evidence_id,
            "session_token": "W9999",
        })
        assert denied_image["reason"] == "owner_authorization_required"
        denied_choice = await invoke(client, "owner_dashboard_choose", {
            "outcome_id": obj.id, "option_id": "style-0",
            "expected_revision": 1, "session_token": "W9999",
        })
        assert denied_choice["reason"] == "owner_authorization_required"
        assert (await invoke(client, "owner_dashboard_login",
                             {"password": "wrong"}))["reason"] == "owner_authorization_required"
        grant = await invoke(client, "owner_dashboard_login", {"password": "test-password"})
        assert grant["status"] == "ready"
        token = grant["session_token"]
        assert token.startswith("dash-") and len(token) == 48
        # Bite: previously this token entered the global OwnerSessions grant map,
        # so fleet_owner accepted it and reached command validation. A dashboard
        # capability must not authorize ANY owner-pult command.
        owner_response = await client.call_tool(
            "fleet_owner", {"agent": token, "command": "intentionally-invalid", "args": []}
        )
        owner_text = " ".join(part.text for part in owner_response.content if hasattr(part, "text"))
        assert "роль владельца не выдана" in owner_text
        args = {"session_token": token}
        accepted = await invoke(client, "owner_dashboard_overview", args)
        assert accepted["status"] == "ready"
        assert accepted["intents"][0]["results"][0]["title"] == "Public title"
        assert "Private taste question" not in str(accepted)
        monkeypatch.setattr(server, "MAX_RESPONSE_CHARS", 200)
        large = await invoke(client, "owner_dashboard_overview", args)
        assert large["reason"] == "result_too_large"
        assert large["intents"] == []
        gallery = await invoke(client, "owner_dashboard_gallery", {
            "outcome_id": obj.id, **args,
        })
        assert gallery["status"] == "ready"
        visual = await invoke(client, "owner_dashboard_image", {
            "outcome_id": obj.id, "image_proof_id": choices[0].image_evidence_id,
            **args,
        })
        assert visual["mime"] == "image/png"
        assert base64.b64decode(visual["base64"]) == examples[0].read_bytes()
        invalid = await invoke(client, "owner_dashboard_image", {
            "outcome_id": obj.id, "image_proof_id": "not-the-real-proof",
            **args,
        })
        assert invalid["reason"] == "unverified_image"
        outside = tmp_path / "not-in-project.png"
        outside.write_bytes(examples[1].read_bytes())
        with store.tx() as tx:
            tx.change("UPDATE evidence SET ref=? WHERE id=?",
                      (str(outside), choices[1].image_evidence_id),
                      event="test.untrusted_proof_path")
        denied_outside = await invoke(client, "owner_dashboard_image", {
            "outcome_id": obj.id, "image_proof_id": choices[1].image_evidence_id,
            **args,
        })
        assert denied_outside["reason"] == "unverified_image"
        choice = await invoke(client, "owner_dashboard_choose", {
            "outcome_id": obj.id, "option_id": "style-0", "expected_revision": 1,
            **args,
        })
        assert choice["status"] == "chosen"
        assert DecisionBook(store).get(choice["decision_id"]).choice == "style-0"
