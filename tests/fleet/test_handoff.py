"""TZ-28: post-delivery packet, hostile inputs and offline Windows toast outbox."""
from __future__ import annotations

import base64
import json
import sys
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from dev_mcp.fleet.handoff import HandoffService, build_packet, validate_overview_url
from dev_mcp.fleet.delivery import AcceptedEvent, on_accept
from dev_mcp.fleet.outcome import Outcome
from dev_mcp.fleet.windows_notify import WindowsToast, toast_xml


def test_owner_decision_toast_does_not_claim_a_finished_result():
    xml=toast_xml('http://127.0.0.1:8766/owner/overview',notice_kind='needs')
    assert 'решение' in xml and 'Результат готов' not in xml
from dev_mcp.owner.actions import ActionRequest
from dev_mcp.owner.decision_book import SourceRef


VERSION = "a" * 64
BEFORE = "b" * 64
AFTER = "c" * 64
KNOWN_GOOD = "d" * 64
OP_REF = "opref:" + "e" * 64
FINGERPRINT = "f" * 64
LOCAL = "http://127.0.0.1:8766/owner/overview"
EXTENSION = "chrome-extension://" + "a" * 32 + "/dashboard/index.html"


def outcome(*, title="Release", state="done", version=VERSION, public=True):
    return Outcome(
        id="outcome_7", parent_id=None, project="Demo", title=title,
        intent_id="intent_7", spec={"dashboard_visibility": "public"} if public else {},
        evidence_spec={"subject_version": version}, authority={"level": "A1"},
        state=state, rev=4, created_ts="2026-10-09T00:00:00+00:00",
        closed_ts="2026-10-09T00:01:00+00:00",
    )


def proof(op, kind):
    return {"kind": kind, "ref": OP_REF, "subject_version": VERSION,
            "env_version": AFTER, "verdict": "pass", "producer": "operator",
            "operation": op, "request_fingerprint": FINGERPRINT,
            "observed_ts": 100, "metrics": {"steps": 1}}


def delivered():
    return {
        "event_id": "acceptance_01", "project": "Demo", "status": "delivered",
        "subject_version": VERSION, "expected_env_version": BEFORE,
        "env_version": AFTER, "known_good_revision": KNOWN_GOOD,
        "evidence": [
            {"stage": "apply", "evidence": proof("cloud.deploy", "delivery")},
            {"stage": "verify", "evidence": proof("ci.run", "run")},
        ],
        "accepted_evidence": {"kind": "review", "ref": "server_review_1",
                              "subject_version": VERSION, "env_version": BEFORE,
                              "verdict": "pass", "producer": "server"},
        "error_code": None, "repair_work": None,
    }


def action(cause="owner_physical", version=VERSION):
    return ActionRequest(
        outcome_id="outcome_7", subject_version=version, cause=cause,
        title="Разблокировать экран", proposed_choice="Разблокировать устройство",
        reason="Требуется присутствие человека",
        evidence=(SourceRef("delivery", "proof-1", version),),
    )


def packet(**kw):
    values = {"outcome": outcome(), "delivery_receipt": delivered(),
              "use_location": "Тестовая панель", "overview_url": LOCAL}
    values.update(kw)
    return build_packet(**values)


def service(tmp_path: Path, *, ready=True, notify=None):
    return HandoffService(
        tmp_path / "handoff.sqlite", session_ready=lambda: ready,
        notifier=notify or (lambda _url: True),
    )


def publish(s, **kw):
    values = {"outcome": outcome(), "delivery_receipt": delivered(),
              "use_location": "Тестовая панель", "overview_url": LOCAL}
    values.update(kw)
    return s.publish(**values)


def test_packet_has_one_versioned_handoff_and_verifiable_delivery_path():
    p = packet()
    assert p["schema"] == 1 and len(p["packet_id"]) == 64
    assert p["result"] == {"title": "Release", "version": VERSION, "outcome_rev": 4}
    assert p["where_to_use"] == "Тестовая панель"
    assert p["delivery"]["status"] == "delivered"
    assert p["delivery"]["before_env"] == BEFORE
    assert p["delivery"]["after_env"] == AFTER
    path = p["delivery"]["verified_path"]
    assert [(x["stage"], x["operation"]) for x in path] == [
        ("apply", "cloud.deploy"), ("verify", "ci.run")]
    assert all(x["subject_version"] == VERSION and x["env_version"] == AFTER
               and x["ref"] == OP_REF for x in path)
    assert p["evidence_summary"]["acceptance"]["subject_version"] == VERSION
    assert p["rollback"] == {"known_good_revision": KNOWN_GOOD,
                             "status": "configured_not_executed"}
    assert p["physical_action"] is None


def test_one_optional_owner_only_physical_action():
    p = packet(physical_action=action())
    assert p["physical_action"]["what"] == "Разблокировать устройство"
    assert len(p["physical_action"]["id"]) == 64
    with pytest.raises(ValueError, match="invalid_physical_action"):
        packet(physical_action=[action(), action()])
    with pytest.raises(ValueError, match="invalid_physical_action"):
        packet(physical_action=action("owner_taste"))
    with pytest.raises(ValueError, match="invalid_physical_action"):
        packet(physical_action=action(version=KNOWN_GOOD))


@pytest.mark.parametrize("bad", [None, {}, {"status": "failed"},
                                 {"status": "rollback_failed"},
                                 {"status": "repair_pending"}])
def test_no_handoff_without_proved_delivery(bad):
    with pytest.raises(ValueError, match="delivery_proof_required"):
        packet(delivery_receipt=bad)


@pytest.mark.parametrize("state", ["open", "dropped"])
def test_cancelled_or_open_outcome_never_notified(tmp_path, state):
    calls = []
    s = service(tmp_path, notify=lambda url: calls.append(url) or True)
    with pytest.raises(ValueError, match="outcome_not_completed"):
        publish(s, outcome=outcome(state=state))
    assert not calls


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(subject_version=KNOWN_GOOD),
    lambda r: r.update(project="wrong"),
    lambda r: r.update(env_version="invalid"),
    lambda r: r.update(expected_env_version=KNOWN_GOOD),
    lambda r: r.update(known_good_revision="invalid"),
    lambda r: r.update(error_code="rollback_failed"),
    lambda r: r.update(repair_work="fix_1"),
])
def test_mismatched_version_or_inconsistent_success_is_rejected(mutate):
    r = delivered()
    mutate(r)
    with pytest.raises(ValueError):
        packet(delivery_receipt=r)


@pytest.mark.parametrize("mutate", [
    lambda r: r["accepted_evidence"].update(producer="agent"),
    lambda r: r["accepted_evidence"].update(subject_version=KNOWN_GOOD),
    lambda r: r["accepted_evidence"].update(verdict="fail"),
    lambda r: r["evidence"].pop(),
    lambda r: r["evidence"][1].update(stage="apply"),
    lambda r: r["evidence"][0]["evidence"].update(producer="agent"),
    lambda r: r["evidence"][1]["evidence"].update(subject_version=KNOWN_GOOD),
    lambda r: r["evidence"][1]["evidence"].update(env_version=BEFORE),
    lambda r: r["evidence"][1]["evidence"].update(verdict="fail"),
    lambda r: r["evidence"][1]["evidence"].update(ref="fake-ref"),
    lambda r: r["evidence"][1]["evidence"].update(operation="cloud.deploy"),
])
def test_unverified_or_wrong_version_evidence_rejected(mutate):
    r = delivered()
    mutate(r)
    with pytest.raises(ValueError):
        packet(delivery_receipt=r)


def test_stale_outcome_version_is_not_result(tmp_path):
    calls = []
    s = service(tmp_path, notify=lambda url: calls.append(url) or True)
    with pytest.raises(ValueError, match="delivery_version_mismatch"):
        publish(s, outcome=outcome(version=KNOWN_GOOD))
    assert calls == []


def test_long_intent_name_and_html_injection_do_not_enter_toast_or_packet_title():
    malicious = "<img src=x onerror=alert(1)>" * 1000
    p = packet(outcome=outcome(title=malicious))
    assert p["result"]["title"] == "Результат готов"
    xml = toast_xml(LOCAL)
    assert "onerror" not in xml and "Release" not in xml and "Demo" not in xml
    assert "Результат готов" in xml
    assert ET.fromstring(xml).attrib["launch"] == LOCAL
    assert packet(outcome=outcome(public=False))["result"]["title"] == "Результат готов"


@pytest.mark.parametrize("url", [
    "https://example.com/owner/overview", "file:///C:/private.txt",
    "http://evil.test/dashboard/", "http://localhost.evil.test:8766/dashboard/",
    "http://127.0.0.1:8766/dashboard/%2e%2e/owner", "http://127.0.0.1:8766/other",
    "http://127.0.0.1:8766/owner/overview?token=secret",
    "http://127.0.0.1:8766/owner/overview#password",
    "http://user:pass@127.0.0.1:8766/owner/overview",
    "http://127.0.0.1:8766/owner/overview\n<script>",
    "javascript:alert(1)", "chrome-extension://evil/dashboard/index.html",
    "chrome-extension://" + "a" * 32 + "/anything.html",
])
def test_deeplink_allowlist_blocks_exfiltration_and_injection(url):
    with pytest.raises(ValueError, match="unsafe_overview_url"):
        validate_overview_url(url)


def test_known_local_and_extension_pages_are_allowed():
    assert validate_overview_url(LOCAL) == LOCAL
    assert validate_overview_url(EXTENSION) == EXTENSION
    assert validate_overview_url("http://localhost:9080/dashboard/")


def test_duplicate_event_is_durable_and_never_spams(tmp_path):
    calls = []
    first = service(tmp_path, notify=lambda url: calls.append(url) or True)
    r1 = publish(first)
    assert r1.notice_state == "submitted" and r1.attempts == 1
    second = service(tmp_path, notify=lambda url: calls.append(url) or True)
    r2 = publish(second)
    assert r2 == r1
    assert second.retry_pending("acceptance_01") == r1
    assert calls == [LOCAL]


def test_event_conflict_never_overwrites_first_packet(tmp_path):
    calls = []
    s = service(tmp_path, notify=lambda url: calls.append(url) or True)
    original = publish(s)
    with pytest.raises(ValueError, match="handoff_event_conflict"):
        publish(s, use_location="other product")
    assert s.get("acceptance_01") == original
    assert calls == [LOCAL]


def test_headless_windows_session_persists_pending_and_can_replay_once(tmp_path):
    calls = []
    headless = service(tmp_path, ready=False, notify=lambda url: calls.append(url) or True)
    first = publish(headless)
    assert first.notice_state == "pending" and first.attempts == 0
    assert publish(headless) == first
    assert calls == []
    resumed = service(tmp_path, notify=lambda url: calls.append(url) or True)
    # Repeated publish cannot spam, even when the session returns.
    assert publish(resumed) == first
    assert resumed.retry_pending("acceptance_01").notice_state == "submitted"
    assert resumed.retry_pending("acceptance_01").attempts == 1
    assert calls == [LOCAL]


def test_failed_or_uncertain_notification_is_not_blindly_resent(tmp_path):
    calls = []
    s = service(tmp_path, notify=lambda url: calls.append(url) or False)
    result = publish(s)
    assert result.notice_state == "pending_unknown" and result.attempts == 1
    assert publish(s) == result and s.retry_pending("acceptance_01") == result
    assert calls == [LOCAL]


def test_transport_exception_is_persisted_without_retry(tmp_path):
    calls = []
    def broken(url):
        calls.append(url)
        raise TimeoutError("might have been delivered")
    s = service(tmp_path, notify=broken)
    assert publish(s).notice_state == "pending_unknown"
    assert s.retry_pending("acceptance_01").notice_state == "pending_unknown"
    assert calls == [LOCAL]


def test_two_publishers_create_only_one_notification(tmp_path):
    calls = []
    s = service(tmp_path, notify=lambda url: calls.append(url) or True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: publish(s), range(2)))
    assert calls == [LOCAL]
    # Racing observer may read a previously committed pending reservation.
    assert {r.notice_state for r in results} <= {"pending", "submitted", "uncertain"}
    assert s.get("acceptance_01").notice_state == "submitted"


def test_no_registered_toast_application_stays_replayable_offline(tmp_path):
    s = HandoffService(tmp_path / "handoff.sqlite", session_ready=lambda: True)
    assert publish(s).notice_state == "pending"
    assert s.get("acceptance_01").attempts == 0


def test_real_verified_delivery_produces_one_offline_packet(tmp_path):
    """Actual TZ-13 on_accept -> TZ-28 handoff, no network or GUI needed."""
    root = json.dumps(str(tmp_path))
    (tmp_path / "deliver.toml").write_text(f"""
[delivery]
version = 1
target = "stage"
workspace = {root}
timeout_seconds = 30
known_good_revision = "{KNOWN_GOOD}"
[apply]
operation = "cloud.deploy"
[apply.params]
workspace = {root}
target = "stage"
artifact_sha256 = "{VERSION}"
release_id = "accepted_release"
[verify]
operation = "ci.run"
[verify.params]
workspace = {root}
target = "stage"
commit_sha = "{VERSION}"
workflow = "postdeploy"
[rollback]
operation = "cloud.deploy"
[rollback.params]
workspace = {root}
target = "stage"
artifact_sha256 = "{KNOWN_GOOD}"
release_id = "last_good"
""", encoding="utf-8")
    current_env = [BEFORE]
    operations = []

    def operator(operation, params):
        operations.append(operation)
        if operation == "cloud.deploy":
            current_env[0] = AFTER
        return {"ok": True, "evidence": proof(
            operation, "delivery" if operation == "cloud.deploy" else "run")}

    event = AcceptedEvent(
        event_id="acceptance_01", project="Demo", subject_version=VERSION,
        env_version=BEFORE, evidence={"kind": "review", "ref": "server_review_1",
                                      "subject_version": VERSION, "env_version": BEFORE,
                                      "verdict": "pass", "producer": "server"},
    )
    delivery = on_accept(
        event, project_root=tmp_path, allowed_roots=(tmp_path,),
        state_db=tmp_path / "delivery.sqlite", fleet_op=operator,
        probe_environment=lambda *_: current_env[0],
        create_defect=lambda *_: pytest.fail("unexpected defect"),
    )
    assert delivery["status"] == "delivered"
    assert operations == ["cloud.deploy", "ci.run"]
    outbox = HandoffService(tmp_path / "handoff.sqlite", session_ready=lambda: False,
                            notifier=lambda *_: pytest.fail("offline GUI"))
    receipt = publish(outbox, delivery_receipt=delivery)
    assert receipt.notice_state == "pending" and receipt.attempts == 0
    assert receipt.packet["result"]["version"] == VERSION
    assert receipt.packet["delivery"]["after_env"] == AFTER
    assert outbox.get("acceptance_01") == receipt


def test_winrt_renderer_uses_registered_app_id_and_never_opens_browser(monkeypatch):
    import dev_mcp.fleet.windows_notify as module

    calls = []
    monkeypatch.setattr(module, "gui_session_available", lambda: True)
    monkeypatch.setattr(module.subprocess, "run", lambda argv, **kwargs:
                        calls.append((argv, kwargs)) or SimpleNamespace(returncode=0))
    win = WindowsToast("Fleet.Owner.Notify")
    assert win.show(LOCAL) is True
    argv, kwargs = calls[0]
    assert argv[:3] == ["powershell.exe", "-NoProfile", "-NonInteractive"]
    script = base64.b64decode(argv[-1]).decode("utf-16-le")
    assert "CreateToastNotifier('Fleet.Owner.Notify')" in script
    assert "ShellExecute" not in script and "Start-Process" not in script
    assert "<toast" in script and LOCAL in script
    assert kwargs["timeout"] == 12
    with pytest.raises(ValueError):
        WindowsToast("Fleet'; Start-Process calc; '")


def test_windows_toast_headless_never_invokes_subprocess(monkeypatch):
    import dev_mcp.fleet.windows_notify as module

    monkeypatch.setattr(module, "gui_session_available", lambda: False)
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k:
                        pytest.fail("headless subprocess must not run"))
    assert WindowsToast("Fleet.Owner.Notify").show(LOCAL) is False


def test_windows_toasts_disabled_stay_pending_until_enabled(tmp_path, monkeypatch):
    """A visible desktop does not imply the OS allows toast delivery."""
    import dev_mcp.fleet.windows_notify as module

    setting = {"enabled": 0}
    fake_winreg = SimpleNamespace(
        HKEY_CURRENT_USER=0,
        REG_DWORD=4,
        OpenKey=lambda *_: nullcontext(object()),
        QueryValueEx=lambda *_: (setting["enabled"], 4),
    )
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(module, "ctypes", SimpleNamespace(
        windll=SimpleNamespace(user32=SimpleNamespace(GetShellWindow=lambda: 1)),
    ))

    calls = []
    s = HandoffService(
        tmp_path / "handoff.sqlite",
        session_ready=module.gui_session_available,
        notifier=lambda url: calls.append(url) or True,
    )
    assert module.gui_session_available() is False
    original = publish(s)
    assert original.notice_state == "pending" and original.attempts == 0
    assert calls == []

    setting["enabled"] = 1
    resumed = s.retry_pending("acceptance_01")
    assert resumed.notice_state == "submitted" and resumed.attempts == 1
    assert calls == [LOCAL]
    assert s.retry_pending("acceptance_01") == resumed
    assert calls == [LOCAL]


def test_public_packet_can_be_serialized_without_private_title(tmp_path):
    s = service(tmp_path, ready=False)
    r = publish(s, outcome=outcome(title="<script>steal()</script>", public=False))
    stored = json.dumps(r.packet, ensure_ascii=False)
    assert "<script>" not in stored
    assert r.notice_state == "pending" and r.packet["overview_url"] == LOCAL
