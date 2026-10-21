"""Общие настройки тестов.

Парковка fleet_take (dev_mcp.fleet.park) в тестах выключена по умолчанию: иначе каждый
fleet_take на пустой очереди у прогретого воркера ждал бы hold_s секунд. Её собственные
тесты создают Parking явно.
"""
import os
import pytest

os.environ.setdefault('DEV_MCP_FLEET_PARK_MAX_PARKED', '0')
# run_status ждёт завершения фоновой команды до 25 с; в тестах по умолчанию не ждёт.
os.environ.setdefault('DEV_MCP_RUN_STATUS_WAIT_S', '0')
# Оракул Jev (fleet/jev.py) в тестах не ходит в сеть, даже если ключ есть в окружении разработчика.
os.environ.pop('TYPESAFE_API_KEY', None)
os.environ.pop('DEV_MCP_JEV_KEY_FILE', None)
os.environ['DEV_MCP_JEV_FROM_CLAUDE'] = '0'
# Руль «сдача только с зелёной приёмкой» (fleet/queue.py) — в тестах выключен; свои тесты включают его.
os.environ.setdefault('DEV_MCP_FLEET_REQUIRE_GREEN_SUBMIT', '0')
# Датчики мира (fleet/world.py) в тестах не опрашивают живой adb этой машины.
os.environ['DEV_MCP_FLEET_SENSORS'] = '0'
# Host-мьютекс гейтов (world.hold_blocking) в тестах выключен: живые прогоны этой машины не ждём.
os.environ.setdefault('DEV_MCP_FLEET_GATE_LOCK', '0')


@pytest.fixture(autouse=True)
def isolated_platform_mode(monkeypatch):
    """Legacy registry fixtures are independent of the operator's live mode.

    Tests for manual maintenance explicitly enable it. Manifest tests read the
    actual configuration, so retiring the project remains covered separately.
    """
    from dev_mcp.fleet import platform_manual
    monkeypatch.setattr(platform_manual, 'enabled', lambda: False)


@pytest.fixture(autouse=True)
def isolated_owner_channel(monkeypatch, tmp_path):
    """No test reaches the owner: 31 «demo — needs» entries of test_owner_inbox landed
    in the live C:\\hub\\_todo\\owner-inbox.md (13.10.2026), and a real token would
    also have created TickTick tasks. Tests that check the channel patch it themselves."""
    from dev_mcp.intents import owner_channel
    monkeypatch.setattr(owner_channel, 'INBOX_PATH', tmp_path / 'owner-inbox.md')
    from types import SimpleNamespace
    silent = SimpleNamespace(show=lambda *args, **kwargs: False)
    monkeypatch.setattr(owner_channel, '_toast', lambda: silent)
    # The server's own TickTick grant (data/secrets) must never reach a test either.
    from dev_mcp.owner import ticktick_oauth
    monkeypatch.setattr(ticktick_oauth, 'SECRET_PATH', tmp_path / 'no-ticktick-grant.json')
    # Owner cards from TickTick are written into the intent inbox: never the live one in a test.
    from dev_mcp.fleet import controller
    inbox = tmp_path / '_isolated_intent_inbox'
    inbox.mkdir(exist_ok=True)
    monkeypatch.setattr(controller, 'INTENT_INBOX', inbox)
