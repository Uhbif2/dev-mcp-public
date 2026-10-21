"""Парковка агента на пустой очереди: ход не кончается, будка не тратится.

Зачем (26.09.2026). Параллельность флота упирается не в dev-mcp, а в действия аккаунта
ChatGPT: около 40–60 «будок» (новый чат / сообщение во вкладку) в час. Один ход агента длится
медианно 4–6 минут, поэтому одновременно работали в среднем 4–10 агентов, хотя вкладок было 14.
Ход кончается, когда модели нечего делать: `fleet_take` ответил «очередь пуста». Следующая
работа стоит новой будки.

Парковка переворачивает это: прогретый агент (уже делал задачи) на пустой очереди получает
ответ не сразу — вызов ждёт до `hold_s` секунд, пока не появится задача, а если не появилась,
велит тут же вызвать `fleet_take` снова. Будка тратится одна на весь ход, а не на задачу.

Границы:
- ожидание асинхронное: поток пула FastMCP не занят (синхронный sleep съел бы пул);
- `hold_s` < `exec_sync_budget_s` (35) < WORKER_LOCAL_MCP_TIMEOUT (45): шлюз не рвёт вызов;
- одновременно ждут не больше `max_parked` — каждый ждущий вызов держит полосу воркера шлюза;
- серия ожидания не длиннее `max_streak_s`: дальше агент завершает ход, будит его будка;
- холодный чат (ни одной задачи) не паркуется: его открыли зря, держать незачем.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

import anyio

Take = Callable[[str, bool], tuple[str, str]]
Record = Callable[[str, dict[str, Any]], None]
Mail = Callable[[str, str], int]  # (agent, since_iso) -> сколько новых сообщений


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, '') or default)
    except ValueError:
        return default


class Parking:
    def __init__(
        self,
        *,
        hold_s: float | None = None,
        poll_s: float = 3.0,
        max_streak_s: float | None = None,
        max_parked: int | None = None,
        idle_reset_s: float = 120.0,
        maintain_every_s: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.hold_s = hold_s if hold_s is not None else _env_float('DEV_MCP_FLEET_PARK_HOLD_S', 30.0)
        self.poll_s = poll_s
        self.max_streak_s = (
            max_streak_s if max_streak_s is not None else _env_float('DEV_MCP_FLEET_PARK_MAX_MIN', 20.0) * 60
        )
        self.max_parked = (
            max_parked if max_parked is not None else int(_env_float('DEV_MCP_FLEET_PARK_MAX_PARKED', 6))
        )
        self.idle_reset_s = idle_reset_s
        self.maintain_every_s = maintain_every_s
        self.clock = clock
        self._lock = threading.Lock()
        self._parked: set[str] = set()
        self._streak: dict[str, tuple[float, float, int]] = {}  # agent -> (start, last_call, calls)

    def parked(self) -> list[str]:
        with self._lock:
            return sorted(self._parked)

    async def wait(
        self, agent: str, empty_payload: str, take: Take, record: Record, mail: Mail | None = None,
    ) -> tuple[str, str]:
        now = self.clock()
        since_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with self._lock:
            if self.max_parked <= 0 or agent in self._parked or len(self._parked) >= self.max_parked:
                return 'empty', empty_payload
            start, last, calls = self._streak.get(agent, (now, now, 0))
            if now - last > self.idle_reset_s:
                start, calls = now, 0
            if now - start >= self.max_streak_s:
                self._streak.pop(agent, None)
                done = True
            else:
                self._streak[agent] = (start, now, calls + 1)
                self._parked.add(agent)
                done = False
        if done:
            record(agent, {'outcome': 'streak_end', 'streak_s': round(now - start), 'calls': calls})
            return 'final', (
                f'Ожидание исчерпано: {round((now - start) / 60)} мин без работы. Одной строкой отчитайся '
                'и заверши ход — когда работа появится, тебя разбудят.'
            )

        t0 = self.clock()
        last_maintain = t0
        try:
            while self.clock() - t0 < self.hold_s:
                await anyio.sleep(self.poll_s)
                maintain = self.clock() - last_maintain >= self.maintain_every_s
                if maintain:
                    last_maintain = self.clock()
                if mail is not None and await anyio.to_thread.run_sync(mail, agent, since_iso):
                    record(agent, {'outcome': 'mail', 'waited_s': round(self.clock() - t0, 1)})
                    return 'mail', (
                        'Пока ты ждал, пришло сообщение — оно в конверте выше. Выполни его '
                        '(и подтверди через fleet_ack), затем снова вызови fleet_take(agent="'
                        + agent + '").'
                    )
                status, payload = await anyio.to_thread.run_sync(take, agent, maintain)
                if status == 'task':
                    with self._lock:
                        self._streak.pop(agent, None)
                    record(agent, {
                        'outcome': 'task', 'waited_s': round(self.clock() - t0, 1),
                        'streak_s': round(self.clock() - start), 'calls': calls + 1,
                    })
                    return 'task', payload
                if status != 'empty':
                    return status, payload
        finally:
            with self._lock:
                self._parked.discard(agent)
        waited_min = (self.clock() - start) / 60
        record(agent, {'outcome': 'hold', 'streak_s': round(self.clock() - start), 'calls': calls + 1})
        return 'parked', (
            f'Готовых задач пока нет — ты в режиме ожидания ({waited_min:.0f} из '
            f'{self.max_streak_s / 60:.0f} мин). СРАЗУ снова вызови fleet_take(agent="{agent}"). '
            'Не пиши итог и не завершай ход: новая задача придёт ответом на один из следующих вызовов.'
        )
