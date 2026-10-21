from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet import owner
from dev_mcp.fleet.owner import OwnerSessions, generate_password, secret_path, set_password, verify_password


def test_password_is_stored_only_as_hash(tmp_path: Path) -> None:
    path = secret_path(tmp_path)
    password = generate_password()
    set_password(path, password)
    stored = path.read_text(encoding="utf-8")
    assert password not in stored
    assert json.loads(stored)["algo"] == "scrypt"
    assert verify_password(path, password)
    assert verify_password(path, f"  {password}\n")  # вставили с пробелами
    assert not verify_password(path, password + "x")
    assert path.name == "owner-secret.json" and "secret" in path.name  # под deny-маской *secret*


def test_short_password_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        set_password(secret_path(tmp_path), "short")


def test_unlock_without_password_file_says_how_to_set_it(tmp_path: Path) -> None:
    sessions = OwnerSessions(secret_file=secret_path(tmp_path))
    ok, message = sessions.unlock("W1", "anything-long-enough")
    assert not ok and "owner-password" in message
    assert not sessions.is_owner("W1")


def test_session_lives_until_ttl_and_lock(tmp_path: Path) -> None:
    now = [1000.0]
    path = secret_path(tmp_path)
    set_password(path, "correct-horse-battery")
    sessions = OwnerSessions(secret_file=path, ttl_s=60, clock=lambda: now[0])
    assert sessions.unlock("W1", "correct-horse-battery")[0]
    assert sessions.is_owner("W1") and not sessions.is_owner("W2")
    now[0] += 61
    assert not sessions.is_owner("W1")
    assert sessions.unlock("W1", "correct-horse-battery")[0]
    sessions.lock("W1")
    assert not sessions.is_owner("W1")


def test_too_many_wrong_passwords_lock_unlock_for_everyone(tmp_path: Path) -> None:
    now = [1000.0]
    path = secret_path(tmp_path)
    set_password(path, "correct-horse-battery")
    sessions = OwnerSessions(secret_file=path, clock=lambda: now[0])
    for _ in range(owner.MAX_FAILURES - 1):
        assert not sessions.unlock("W9", "guess-guess-guess")[0]
    ok, message = sessions.unlock("W9", "guess-guess-guess")
    assert not ok and "закрыта" in message
    ok, message = sessions.unlock("W1", "correct-horse-battery")  # даже верный — в замке
    assert not ok and "закрыта" in message
    now[0] += owner.LOCKOUT_S + 1
    assert sessions.unlock("W1", "correct-horse-battery")[0]


def test_local_grant_gives_role_without_password_and_survives_restart(tmp_path: Path) -> None:
    # 23.09.2026: пароль в аргументе инструмента ChatGPT не пропускал — роль выдаётся локально
    secret = secret_path(tmp_path)  # пароль не задан вовсе
    now = [1000.0]
    owner.grant(owner.grants_path(secret), "W7", ttl_s=60, now=now[0])
    assert owner.grants_path(secret).name.count("secret") == 1  # под маской *secret*

    fresh = OwnerSessions(secret_file=secret, clock=lambda: now[0])  # «после перезапуска»
    assert fresh.is_owner("W7") and not fresh.is_owner("W8")
    assert fresh.active() == ["W7"]
    now[0] += 61
    assert not fresh.is_owner("W7")

    owner.grant(owner.grants_path(secret), "W7", ttl_s=60, now=now[0])
    fresh.lock("W7")  # lock снимает и выдачу
    assert not fresh.is_owner("W7")
    assert owner.revoke_grant(owner.grants_path(secret)) == []
