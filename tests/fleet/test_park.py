"""Парковка fleet_take на пустой очереди (dev_mcp.fleet.park)."""
from __future__ import annotations

import sys
from pathlib import Path

import anyio
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

from dev_mcp.fleet.park import Parking  # noqa: E402


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def make(**kw):
    clock = Clock()
    kw.setdefault('hold_s', 30.0)
    kw.setdefault('max_streak_s', 1200.0)
    kw.setdefault('max_parked', 6)
    parking = Parking(poll_s=0.0, clock=clock, **kw)
    return parking, clock


def run(coro_fn):
    return anyio.run(coro_fn)


def test_task_arrives_during_hold() -> None:
    parking, clock = make()
    events = []
    calls = {'n': 0}

    def take(agent, maintain):
        calls['n'] += 1
        clock.t += 5
        return ('task', 'ЗАДАЧА') if calls['n'] == 3 else ('empty', 'пусто')

    status, payload = run(lambda: parking.wait('W1', 'пусто', take, lambda a, p: events.append(p)))
    assert (status, payload) == ('task', 'ЗАДАЧА')
    assert events[-1]['outcome'] == 'task'
    assert parking.parked() == []


def test_hold_expires_with_instruction_to_call_again() -> None:
    parking, clock = make(hold_s=10.0)

    def take(agent, maintain):
        clock.t += 4
        return 'empty', 'пусто'

    status, payload = run(lambda: parking.wait('W7', 'пусто', take, lambda a, p: None))
    assert status == 'parked'
    assert 'fleet_take(agent="W7")' in payload and 'не завершай ход' in payload


def test_disabled_or_full_returns_plain_empty() -> None:
    parking, _ = make(max_parked=0)
    never = lambda a, m: (_ for _ in ()).throw(AssertionError('take must not be called'))  # noqa: E731
    assert run(lambda: parking.wait('W1', 'пусто', never, lambda a, p: None)) == ('empty', 'пусто')


def test_streak_limit_ends_turn() -> None:
    parking, clock = make(hold_s=10.0, max_streak_s=25.0)
    events = []

    def take(agent, maintain):
        clock.t += 5
        return 'empty', 'пусто'

    statuses = []
    for _ in range(4):
        status, _payload = run(lambda: parking.wait('W1', 'пусто', take, lambda a, p: events.append(p)))
        statuses.append(status)
        clock.t += 1  # агент сразу зовёт снова
    assert statuses[:3] == ['parked', 'parked', 'parked']
    assert statuses[3] == 'final'
    assert events[-1]['outcome'] == 'streak_end'


def test_idle_gap_starts_new_streak() -> None:
    parking, clock = make(hold_s=10.0, max_streak_s=25.0)

    def take(agent, maintain):
        clock.t += 5
        return 'empty', 'пусто'

    for _ in range(3):
        run(lambda: parking.wait('W1', 'пусто', take, lambda a, p: None))
    clock.t += 600  # ход кончился давно; новая будка — новая серия
    assert run(lambda: parking.wait('W1', 'пусто', take, lambda a, p: None))[0] == 'parked'


def test_concurrency_cap() -> None:
    parking = Parking(hold_s=0.3, poll_s=0.05, max_streak_s=60, max_parked=1)
    results = {}

    def take(agent, maintain):
        return 'empty', 'пусто'

    async def both():
        async def one(agent, delay):
            await anyio.sleep(delay)
            results[agent] = await parking.wait(agent, 'пусто', take, lambda a, p: None)

        async with anyio.create_task_group() as tg:
            tg.start_soon(one, 'W1', 0)
            tg.start_soon(one, 'W2', 0.1)

    anyio.run(both)
    assert results['W1'][0] == 'parked'
    assert results['W2'] == ('empty', 'пусто')


def test_mcp_tool_parks_warm_worker_and_hands_new_task(tmp_path: Path, monkeypatch) -> None:
    """Сквозь настоящий FastMCP: прогретый воркер на пустой очереди ждёт и получает задачу,
    появившуюся во время ожидания; холодный не паркуется."""
    import asyncio
    import json
    import threading
    import time as _time
    from datetime import datetime, timezone

    monkeypatch.setenv('DEV_MCP_FLEET_PARK_MAX_PARKED', '2')
    monkeypatch.setenv('DEV_MCP_FLEET_PARK_HOLD_S', '8')
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_tools import _make_server, _seed_task, _tool_fn  # noqa: E402

    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root, tz='TZ-A')
    join = _tool_fn(server, 'fleet_join')
    take = _tool_fn(server, 'fleet_take')
    submit = _tool_fn(server, 'fleet_submit')
    assert 'worker_id: W1' in join('warm')
    assert 'worker_id: W2' in join('reviewer')
    assert 'worker_id: W3' in join('cold')
    assert 'demo/TZ-A' in take('W1')
    (root / 'owned.txt').write_text('done', encoding='utf-8')
    assert 'Отказано' not in submit('W1', 'done')
    assert 'TZ-A' in take('W2')  # ревью TZ-A уходит второму, W1 (автор) его не получит

    t0 = _time.monotonic()
    assert 'Очередь пуста' in take('W3')  # холодный: сразу, без парковки
    assert _time.monotonic() - t0 < 2

    def later():
        _time.sleep(1.0)
        now = datetime.now(timezone.utc).isoformat()
        with store.tx() as tx:
            tx.change(
                """INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,
                       resources,acceptance,author_worker,body,created_at,updated_at)
                   VALUES ('demo','TZ-B','B','ready',1,'work','ToDo/TZ-B.md','sha','[]',?,'[]','[]',NULL,'b',?,?)""",
                (json.dumps([str(root / 'b.txt')]), now, now),
                event={'kind': 'seed'},
            )

    threading.Thread(target=later, daemon=True).start()
    t0 = _time.monotonic()
    got = take('W1')
    waited = _time.monotonic() - t0
    # ревью TZ-A может прийти раньше TZ-B, но не W1 (автор) — значит W1 получает TZ-B
    assert 'demo/TZ-B' in got, got[:300]
    assert 1.0 <= waited < 8
    with store.tx() as tx:
        rows = tx.all("SELECT payload FROM events WHERE kind='park'") if hasattr(tx, 'all') else []
    if rows:
        assert json.loads(rows[-1]['payload'])['outcome'] == 'task'


def test_mcp_tool_parked_worker_is_interrupted_by_new_message(tmp_path: Path, monkeypatch) -> None:
    """Руль: сообщение агенту, пока он ждёт, обрывает ожидание за poll_s и приходит в конверте."""
    import threading
    import time as _time

    monkeypatch.setenv('DEV_MCP_FLEET_PARK_MAX_PARKED', '2')
    monkeypatch.setenv('DEV_MCP_FLEET_PARK_HOLD_S', '10')
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_tools import _make_server, _seed_task, _tool_fn  # noqa: E402
    from dev_mcp.fleet import inbox

    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root, tz='TZ-A')
    join = _tool_fn(server, 'fleet_join')
    take = _tool_fn(server, 'fleet_take')
    submit = _tool_fn(server, 'fleet_submit')
    join('warm')
    join('reviewer')
    take('W1')
    (root / 'owned.txt').write_text('done', encoding='utf-8')
    submit('W1', 'done')
    take('W2')

    def later():
        _time.sleep(1.0)
        inbox.send(store, to='W1', from_kind='owner', from_id='', kind='note',
                   body='Отчитайся одной строкой, что сделал за ход.')

    threading.Thread(target=later, daemon=True).start()
    t0 = _time.monotonic()
    got = take('W1')
    waited = _time.monotonic() - t0
    assert 1.0 <= waited < 8, waited
    assert 'Отчитайся одной строкой' in got
    assert 'пришло сообщение' in got


def test_take_with_active_claim_answers_at_once_instead_of_parking(tmp_path, monkeypatch) -> None:
    """27.09: W585 повторно позвал fleet_take с TZ-55 на руках и простоял 31 с на парковке."""
    import time as _time

    from test_tools import _make_server, _seed_task, _tool_fn

    monkeypatch.setenv("DEV_MCP_FLEET_PARK_MAX_PARKED", "6")
    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root)
    _tool_fn(server, "fleet_join")("worker")
    assert "demo/TZ-X" in _tool_fn(server, "fleet_take")("W1")
    t0 = _time.monotonic()
    again = _tool_fn(server, "fleet_take")("W1")
    assert _time.monotonic() - t0 < 5
    assert "уже есть активная задача demo/TZ-X" in again
