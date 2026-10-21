"""Тесты живого перечитывания policy.toml (reload_from / watch_policy_file).

Цель — доказать ровно то, ради чего это писалось: правка policy.toml
долетает до УЖЕ РАБОТАЮЩЕГО объекта Policy без его пересоздания (то есть без
перезапуска процесса dev-mcp), а битый файл эту работающую политику не рушит.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from dev_mcp.policy import (  # noqa: E402
    Policy,
    PolicyError,
    check_policy_file_changed,
    watch_policy_file,
)


def _roots_text(*roots: Path) -> str:
    listed = ', '.join(f"'{r}'" for r in roots)
    return f'roots = [{listed}]\n'


def _touch_forward(path: Path) -> None:
    """Сдвигает mtime заведомо вперёд — независимо от гранулярности ФС."""
    future = time.time() + 5
    os.utime(path, (future, future))


# ---------- reload_from ----------


def test_reload_from_mutates_same_object(tmp_path: Path) -> None:
    ws1 = tmp_path / 'ws1'
    ws1.mkdir()
    cfg = tmp_path / 'policy.toml'
    cfg.write_text(_roots_text(ws1), encoding='utf-8')

    policy = Policy.load(cfg)
    same_object = policy
    assert len(policy.roots) == 1

    ws2 = tmp_path / 'ws2'
    ws2.mkdir()
    cfg.write_text(_roots_text(ws1, ws2), encoding='utf-8')
    policy.reload_from(cfg)

    assert policy is same_object  # не пересоздан — только поля обновлены
    assert len(policy.roots) == 2


def test_reload_from_broken_config_keeps_old_policy(tmp_path: Path) -> None:
    ws1 = tmp_path / 'ws1'
    ws1.mkdir()
    cfg = tmp_path / 'policy.toml'
    cfg.write_text(_roots_text(ws1), encoding='utf-8')

    policy = Policy.load(cfg)
    original_roots = list(policy.roots)

    cfg.write_text('this is [[[ not valid toml', encoding='utf-8')
    with pytest.raises(PolicyError):
        policy.reload_from(cfg)

    assert policy.roots == original_roots  # старая политика не тронута ни на йоту


# ---------- check_policy_file_changed ----------


def test_first_check_is_not_a_change(tmp_path: Path) -> None:
    ws1 = tmp_path / 'ws1'
    ws1.mkdir()
    cfg = tmp_path / 'policy.toml'
    cfg.write_text(_roots_text(ws1), encoding='utf-8')
    policy = Policy.load(cfg)

    last_mtime, changed, ok, message = check_policy_file_changed(policy, cfg, None)
    assert changed is False
    assert ok is True
    assert last_mtime is not None
    assert message == ''


def test_check_detects_and_applies_edit(tmp_path: Path) -> None:
    ws1, ws2 = tmp_path / 'ws1', tmp_path / 'ws2'
    ws1.mkdir()
    ws2.mkdir()
    cfg = tmp_path / 'policy.toml'
    cfg.write_text(_roots_text(ws1), encoding='utf-8')
    policy = Policy.load(cfg)

    last_mtime, _, _, _ = check_policy_file_changed(policy, cfg, None)
    cfg.write_text(_roots_text(ws1, ws2), encoding='utf-8')
    _touch_forward(cfg)

    new_mtime, changed, ok, message = check_policy_file_changed(policy, cfg, last_mtime)
    assert changed is True
    assert ok is True
    assert new_mtime != last_mtime
    assert len(policy.roots) == 2


def test_check_detects_broken_edit_without_crashing(tmp_path: Path) -> None:
    ws1 = tmp_path / 'ws1'
    ws1.mkdir()
    cfg = tmp_path / 'policy.toml'
    cfg.write_text(_roots_text(ws1), encoding='utf-8')
    policy = Policy.load(cfg)

    last_mtime, _, _, _ = check_policy_file_changed(policy, cfg, None)
    cfg.write_text('broken [[[', encoding='utf-8')
    _touch_forward(cfg)

    _, changed, ok, message = check_policy_file_changed(policy, cfg, last_mtime)
    assert changed is True
    assert ok is False
    assert message  # текст ошибки не пуст
    assert len(policy.roots) == 1  # старая политика цела


# ---------- watch_policy_file (реальный фоновый поток) ----------


def test_watch_policy_file_applies_edit_in_background_thread(tmp_path: Path) -> None:
    ws1, ws2 = tmp_path / 'ws1', tmp_path / 'ws2'
    ws1.mkdir()
    ws2.mkdir()
    cfg = tmp_path / 'policy.toml'
    cfg.write_text(_roots_text(ws1), encoding='utf-8')
    policy = Policy.load(cfg)

    stop = threading.Event()
    events: list[tuple[bool, str]] = []
    t = threading.Thread(
        target=watch_policy_file,
        args=(policy, cfg),
        kwargs={'interval_s': 0.05, 'on_change': lambda ok, msg: events.append((ok, msg)), 'stop_event': stop},
        daemon=True,
    )
    t.start()
    try:
        time.sleep(0.1)
        cfg.write_text(_roots_text(ws1, ws2), encoding='utf-8')
        _touch_forward(cfg)

        deadline = time.time() + 3
        while time.time() < deadline and len(policy.roots) < 2:
            time.sleep(0.05)
    finally:
        stop.set()
        t.join(timeout=2)

    assert len(policy.roots) == 2  # подхвачено без пересоздания процесса/объекта
    assert events and events[-1] == (True, 'policy.toml перечитан')
