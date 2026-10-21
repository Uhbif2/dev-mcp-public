"""Автоматический разбор заявок request_note — без человека.

25.09.2026: владелец заявки вручную не разбирает и не будет. За 22-24.09 в очереди
review.cmd скопилось 127 решений, из них 83 заявки kind=other («одобри черновик»,
«заморозь», «реактивируй») — владелец отклонял их пачкой без причины, а агенты
приходили с той же просьбой снова и держали вкладку в ожидании. Теперь решение
принимается в момент вызова request_note и сразу возвращается агенту:

- доступ, который периметр УЖЕ даёт (путь внутри roots/write_roots/exec_roots, бинарь
  из exec_allowed_bin, сеть при allow_network_fetch) — approved, с объяснением, почему
  у агента могло не получиться (deny_patterns, чужой захват, cwd вне exec_roots);
- всё, что расширяет периметр, — denied с рецептом: чем обойтись внутри периметра.
  Периметр (policy.toml) автомат не расширяет никогда: это решение владельца, и
  агент web-ChatGPT не должен уметь выпросить себе новый исполняемый файл;
- kind=other к владельцу — denied с рецептом: чужая правка — заявкой в очередь
  (to='<проект>/<tz>' или 'queue', задача создаётся сразу), решение внутри цели
  ТЗ — самому. Явный blocker request_note паркует source claim автоматически; для
  blocker без создаваемой dependency используется fleet_finish(summary='blocked: ...').

Модуль общий для сервера (request_note) и tools/review_requests.py (добор старой
очереди), поэтому решение одно и то же, где бы его ни приняли.
"""

from __future__ import annotations

import ntpath
import os
from typing import Any

PATH_LISTS = ('roots', 'write_roots', 'exec_roots')
BIN_LISTS = ('exec_allowed_bin', 'exec_args_unrestricted')

OTHER_RECIPE = (
    'Владелец заявки вручную не разбирает — решение принято автоматически. '
    'Нужна правка в чужом пути → request_note(kind="other", to="<проект>/<tz>" или "queue", '
    'path="<путь>", agent=<worker_id>): задача создаётся сразу готовой к выдаче. '
    'Выбор внутри цели своего ТЗ (вариант, граница, формулировка) — принимай сам, обоснование '
    'одной строкой в «Итог». Если чужая правка реально блокирует работу — прямо напиши '
    '«blocked/заблокировано» в summary/detail: request_note сам освободит source claim до '
    'выполнения созданной dependency. Внешний blocker без внутренней задачи — '
    'fleet_finish(agent, summary="blocked: <точная причина>"): задача frozen и не крутится. '
    'Не жди ответа владельца и не повторяй заявку.'
)


def _norm(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(str(path))))


def covered(path: str, roots: list[Any]) -> bool:
    """Лежит ли путь внутри одного из корней (сам корень — тоже внутри)."""
    target = _norm(path)
    for root in roots:
        base = _norm(root)
        if target == base or target.startswith(base.rstrip('\\/') + os.sep):
            return True
    return False


def _stem(name: str) -> str:
    base = os.path.basename(str(name)).lower()
    return base[:-4] if base.endswith('.exe') else base


def _requested_lists(entry: dict[str, Any]) -> list[str]:
    raw = entry.get('lists') or []
    items = raw if isinstance(raw, list) else str(raw).split(',')
    return [str(x).strip() for x in items if str(x).strip()]


def decide(entry: dict[str, Any], policy: Any) -> tuple[str, str]:
    """(decision, note): decision — 'approved' | 'denied'. Периметр не меняется никогда."""
    kind = str(entry.get('kind') or 'other').strip().lower()
    path = str(entry.get('path') or '').strip()
    lists = _requested_lists(entry)

    if kind == 'network':
        if getattr(policy, 'allow_network_fetch', False):
            return 'approved', (
                'Сеть уже разрешена: fetch_url качает публичные адреса в write_roots '
                f'(лимит {getattr(policy, "fetch_max_bytes", 0)} б). Внутренние адреса и localhost '
                'закрыты намеренно — это не меняется.'
            )
        return 'denied', 'Сеть закрыта периметром владельца. ' + OTHER_RECIPE

    if kind == 'bin' or (lists and set(lists) <= set(BIN_LISTS)):
        name = path or str(entry.get('summary') or '')
        allowed = [str(b) for b in getattr(policy, 'exec_allowed_bin', [])]
        if path and not ntpath.isabs(path) and _stem(path) in {_stem(b) for b in allowed}:
            return 'approved', f'«{path}» уже разрешён (exec_allowed_bin). Запускай через run с cwd в exec_roots.'
        tasks = sorted(getattr(policy, 'tasks', {}) or {})
        return 'denied', (
            f'Новые исполняемые файлы автомат не разрешает ({name[:80]}). Разрешены: '
            f'{", ".join(allowed)}; именованные задачи run_task: {", ".join(tasks) or "нет"}. '
            'Обходись ими. Project-local .cmd/.bat внутри exec_root теперь запускаются напрямую через '
            'run(argv=["relative\\script.cmd", ...]) — сырой cmd.exe просить не нужно. Именованный run_task нужен '
            'только для фиксированной системной/shell-команды. Если без нового внешнего бинаря задача объективно '
            'невыполнима — fleet_finish(agent, summary="blocked: нужен внешний бинарь …"), владельца в чате не жди.'
        )

    if kind in ('access', 'root') or (path and ntpath.isabs(path)):
        if not path or not ntpath.isabs(path):
            return 'denied', (
                'В заявке нет абсолютного пути (поле path) — решать не по чему. Если доступ правда '
                'нужен, повтори request_note с path и lists; но сначала проверь policy_info: '
                'корни всех проектов флота уже открыты на чтение, запись и запуск.'
            )
        wanted = [x for x in lists if x in PATH_LISTS] or ['roots', 'write_roots']
        # 0024: внутри tree_roots (кроме tree_deny) запись и запуск разрешены, как и в самих
        # инструментах. 13.10 без этого автомат отказал проекту интенции C:\hub\local-library
        # «вне периметра» и заморозил стадию, хотя записи туда шли.
        in_tree = (covered(path, list(getattr(policy, 'tree_roots', [])))
                   and not covered(path, list(getattr(policy, 'tree_deny', []))))
        have = {
            'roots': in_tree or covered(path, list(getattr(policy, 'roots', []))),
            'write_roots': in_tree or covered(path, list(getattr(policy, 'write_roots', []))),
            'exec_roots': in_tree or covered(path, list(getattr(policy, 'exec_roots', []))),
        }
        missing = [name for name in wanted if not have.get(name)]
        if not missing:
            return 'approved', (
                f'Уже разрешено ({", ".join(wanted)}): {path}. Если операция всё равно отказала, '
                'причина не в периметре: имя попало под deny_patterns (token/secret/key/.env…), '
                'путь держит чужой захват (FLEET_FOREIGN_PATH) или запись вне зоны твоей задачи '
                '(FLEET_OUT_OF_CLAIM) — читай код отказа.'
            )
        return 'denied', (
            f'Путь вне периметра ({", ".join(missing)}): {path}. Автомат периметр не расширяет. '
            'Работай внутри корня своего проекта; если это настоящий blocker — '
            'fleet_finish(agent, summary="blocked: путь вне разрешённого периметра …").'
        )

    return 'denied', OTHER_RECIPE


def triage_pending(entries: list[dict[str, Any]], policy: Any, *, stamp: str) -> int:
    """Проставить решение всем неразобранным заявкам списка. Возвращает сколько разобрано."""
    done = 0
    for entry in entries:
        if entry.get('decision'):
            continue
        decision, note = decide(entry, policy)
        entry['decision'] = decision
        entry['note'] = note
        entry['reviewed_at'] = stamp
        entry['auto'] = True
        done += 1
    return done


__all__ = ['OTHER_RECIPE', 'covered', 'decide', 'triage_pending']
