"""Ресурс одного Android-устройства: чужой подключённый телефон его не поднимает (20.11.2026).

Регрессия (заявка ca9db21e): единый ресурс «телефон» был up при любом устройстве, и задача
под Tablet TAB-A1 выходила в работу, потому что подключён PHONEB.
"""
from __future__ import annotations

from dev_mcp.fleet import world

PHONEB = "0123PHONEB device product:CI8n model:PHONEB_CI8n device:PHONEB-CI8n transport_id:3"
SAMSUNG_OFF = "R52M1234 offline product:gtaxlwifi model:SM_T735 device:gtaxl transport_id:4"
SAMSUNG_WIFI = ("adb-R52M1234-abc._adb-tls-connect._tcp device product:gtaxlwifi "
                "model:SM_T735 device:gtaxl transport_id:5")


def test_other_phone_does_not_raise_device_resource() -> None:
    state, detail = world.device_state("model:SM_T735", [PHONEB])
    assert state == world.DOWN and "PHONEB" in detail


def test_offline_target_is_down_and_shown() -> None:
    state, detail = world.device_state("model:SM_T735", [PHONEB, SAMSUNG_OFF])
    assert state == world.DOWN and "offline" in detail


def test_ready_target_over_wifi_is_up() -> None:
    state, detail = world.device_state("model:sm_t735", [PHONEB, SAMSUNG_OFF, SAMSUNG_WIFI])
    assert state == world.UP and "adb-R52M1234" in detail and "PHONEB" not in detail


def test_bad_regex_and_silent_daemon_fail_closed() -> None:
    assert world.device_state("model:(", [SAMSUNG_WIFI])[0] == world.UNKNOWN
    assert world.device_state("model:SM_T735", None)[0] == world.UNKNOWN


def test_sensor_without_device_keeps_any_device_rule(monkeypatch) -> None:
    monkeypatch.setattr(world, "_adb_devices", lambda *a, **k: (world.UP, "устройства: x"))
    monkeypatch.setattr(world, "_adb_device_lines", lambda *a, **k: [PHONEB])
    assert world._adb_sensor({"sensor": "adb"})[0] == world.UP
    assert world._adb_sensor({"sensor": "adb", "device": "model:SM_T735"})[0] == world.DOWN
