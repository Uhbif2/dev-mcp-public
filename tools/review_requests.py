"""Автоматический дренаж legacy request_note очереди.

Новые request_note сервер разбирает сразу: уже имеющееся право подтверждает,
расширение security-периметра отклоняет с безопасной альтернативой, а scoped
cross-zone request превращает в обычную fleet-задачу. Владелец в этом цикле
не участвует.

Этот файл остаётся мигратором старых строк logs/requests.jsonl. Обычный запуск
(или review.cmd) прогоняет всю legacy-очередь через request_triage, переносит
решённые записи в requests.history.jsonl и очищает pending. policy.toml он
автоматически не расширяет.

Единственный публичный диагностический флаг:
    review_requests.py --list

Старые helper-функции построения proposal сохранены для чтения legacy-формата
и тестов миграции, но ручные --approve/--deny/--interactive из CLI удалены.
"""

from __future__ import annotations

import argparse
import json
import ntpath
import os
import re
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'src'))
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:  # noqa: BLE001 — best-effort, не критично для логики
    pass

from dev_mcp.policy import Policy, PolicyError  # noqa: E402

REQUESTS_PATH = REPO / 'logs' / 'requests.jsonl'
HISTORY_PATH = REPO / 'logs' / 'requests.history.jsonl'
POLICY_PATH = REPO / 'policy.toml'
CANDIDATE_PATH = REPO / 'policy.toml.candidate'
BACKUP_DIR = REPO / 'logs' / 'policy_backups'

# Списки policy.toml, которые этот скрипт умеет дополнять текстовой вставкой.
# deny_patterns/ignore_patterns сюда намеренно не входят — это маски, а не
# пути/бинари, и трогают их гораздо реже; для них правка руками уместнее.
EDITABLE_LISTS = ('roots', 'write_roots', 'exec_roots', 'exec_allowed_bin', 'exec_args_unrestricted')
PATH_LISTS = ('roots', 'write_roots', 'exec_roots')
BIN_LISTS = ('exec_allowed_bin', 'exec_args_unrestricted')

# Эвристика для request_note БЕЗ структурных полей path/lists (старые заявки,
# либо агент поленился их заполнить) — ищем абсолютный Windows-путь и по каким
# словам понятно, куда его добавить. Работает ТОЛЬКО как предложение человеку —
# ревьюер всегда видит и путь, и списки ДО того, как нажать (a), поэтому неточный
# regex-парсинг чужого текста не превращается в дыру: не понравилось — (e) на
# ручной выбор, и ни одна правка не проходит мимо Policy.load() в apply_change.
_PATH_RE = re.compile(r'[A-Za-z]:\\[^\s,;:"\'()]+')


# ---------------------------------------------------------------------------
# logs/requests.jsonl
# ---------------------------------------------------------------------------


def load_requests(path: Path | None = None) -> list[dict]:
    # path=None, а не path=REQUESTS_PATH: значение параметра по умолчанию
    # фиксируется в момент ОПРЕДЕЛЕНИЯ функции — если бы дефолтом была сама
    # REQUESTS_PATH, monkeypatch/переприсвоение модульной переменной в тестах
    # (и в любом другом импортирующем коде) на уже связанный дефолт бы не подействовал.
    path = REQUESTS_PATH if path is None else path
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # битую строку молча пропускаем, а не роняем весь разбор
    return out


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    """Атомарно переписать JSONL, чтобы очередь не оставалась полуфайлом."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = '\n'.join(json.dumps(e, ensure_ascii=False) for e in entries)
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    tmp.write_text(body + ('\n' if entries else ''), encoding='utf-8')
    tmp.replace(path)


def load_history(path: Path | None = None) -> list[dict]:
    return load_requests(HISTORY_PATH if path is None else path)


def save_requests(
    entries: list[dict], path: Path | None = None, history_path: Path | None = None,
) -> None:
    """Сохранить активную очередь, перенеся решения в отдельную историю.

    `requests.jsonl` — именно очередь, поэтому approved/denied там больше не
    остаются. История нужна для request_status(id) и аудита владельца.
    Сначала дописываем историю, затем очищаем очередь: при аварии возможен
    только безвредный повтор, но не потеря решения.
    """
    path = REQUESTS_PATH if path is None else path
    history_path = HISTORY_PATH if history_path is None else history_path
    active = [e for e in entries if not e.get('decision')]
    resolved = [e for e in entries if e.get('decision')]
    if resolved:
        history = load_history(history_path)
        seen = {str(e.get('id')) for e in history if e.get('id')}
        for entry in resolved:
            req_id = str(entry.get('id') or '')
            if not req_id or req_id not in seen:
                history.append(entry)
                if req_id:
                    seen.add(req_id)
        _write_jsonl(history_path, history)
    _write_jsonl(path, active)


def pending(entries: list[dict]) -> list[dict]:
    """Неразобранные заявки — те же объекты, что и в `entries` (не копии):

    правка `mark()` на элементе списка, полученного отсюда, видна и в
    исходном `entries`, поэтому `save_requests(entries)` в конце заберёт
    решение без отдельного шага «найти и обновить по id»."""
    return [e for e in entries if not e.get('decision')]


def mark(req: dict, decision: str, note: str = '') -> None:
    req['decision'] = decision
    req['reviewed_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
    if note:
        req['note'] = note


def find_by_id(entries: list[dict], req_id: str) -> dict | None:
    for e in entries:
        if e.get('id') == req_id:
            return e
    return None


def backfill_ids(entries: list[dict]) -> bool:
    """Заявки, оставленные ДО того, как request_note стал класть id, id не имеют.

    Интерактивный режим им не мешает (работает с тем же объектом в памяти, id не
    нужен), а вот однострочные флаги `--approve ID`/`--deny ID` ищут заявку
    именно по id — без него их было бы не разобрать иначе как интерактивно.
    Возвращает True, если что-то дописано (вызывающий код решает, сохранять ли).
    """
    changed = False
    for e in entries:
        if not e.get('id'):
            e['id'] = uuid.uuid4().hex[:8]
            changed = True
    return changed


# ---------------------------------------------------------------------------
# policy.toml — текстовая правка (не TOML-сериализатор: он бы съел комментарии,
# которые в этом файле — часть инструкции для человека, см. policy.toml).
# ---------------------------------------------------------------------------


def insert_into_list(text: str, list_name: str, value: str) -> str:
    """Дописать `value` последней строкой в `list_name = [...]` policy.toml.

    Ищет строку `{list_name} = [`, считает глубину `[`/`]` до нуля построчно,
    вставляет новую строку прямо перед закрывающей скобкой. Значение — TOML
    literal string (одинарные кавычки, без экранирования) — как и все пути в
    этом файле, поэтому само значение не может содержать `'`.
    """
    if "'" in value:
        raise ValueError("значение не может содержать одинарную кавычку (TOML literal string): " + value)
    if '\n' in value or '\x00' in value:
        raise ValueError('значение не может содержать перевод строки или NUL')
    if not value.strip():
        raise ValueError('пустое значение')

    lines = text.splitlines(keepends=True)
    pattern = re.compile(rf'^\s*{re.escape(list_name)}\s*=\s*\[')
    start = next((i for i, line in enumerate(lines) if pattern.match(line)), None)
    if start is None:
        raise ValueError(
            f'в policy.toml нет списка «{list_name} = [...]» — заведи пустой `{list_name} = []` вручную один раз'
        )

    def value_key(item: str) -> str:
        if list_name in PATH_LISTS:
            return os.path.normcase(os.path.normpath(os.path.abspath(os.path.expanduser(item))))
        stem = item[:-4] if item.lower().endswith('.exe') else item
        return stem.lower()

    depth = lines[start].count('[') - lines[start].count(']')
    if depth <= 0:
        raise ValueError(f'список «{list_name}» должен быть многострочным, чтобы его безопасно дополнить')
    end = start
    while depth > 0:
        end += 1
        if end >= len(lines):
            raise ValueError(f'не нашёл закрывающую «]» для «{list_name}» — файл повреждён?')
        depth += lines[end].count('[') - lines[end].count(']')

    existing_re = re.compile(r"^\s*'([^']*)'\s*,?")
    existing_values = [
        match.group(1)
        for line in lines[start + 1:end]
        if (match := existing_re.match(line))
    ]
    if any(value_key(item) == value_key(value) for item in existing_values):
        return text

    lines.insert(end, f"    '{value}',\n")
    return ''.join(lines)


def append_task_block(
    text: str, name: str, cwd: str, argv: list[str], description: str = '', timeout_s: int = 300,
) -> str:
    """Добавить `[tasks.<name>]` в конец файла — порядок TOML-таблиц не важен."""
    if not re.match(r'^[a-zA-Z0-9_-]+$', name):
        raise ValueError('имя задачи: только латиница/цифры/-/_')
    if f'[tasks.{name}]' in text:
        raise ValueError(f'задача «{name}» уже есть в policy.toml')
    if not argv:
        raise ValueError('argv пуст')
    for value, label in [(cwd, 'cwd'), (description, 'description'), *((a, 'argv') for a in argv)]:
        if "'" in value:
            raise ValueError(f"{label} не может содержать одинарную кавычку: {value}")
        if '\n' in value or '\x00' in value:
            raise ValueError(f'{label} не может содержать перевод строки/NUL')

    argv_toml = ', '.join(f"'{a}'" for a in argv)
    block = (
        f"\n[tasks.{name}]\n"
        f"cwd = '{cwd}'\n"
        f"argv = [{argv_toml}]\n"
        f"description = '{description}'\n"
        f"timeout_s = {int(timeout_s)}\n"
    )
    if not text.endswith('\n'):
        text += '\n'
    return text + block


def validate_candidate(text: str) -> Policy:
    """Проверить кандидата ПЕРЕД записью: временный файл + настоящий Policy.load.

    Так проверка бьёт по тем же правилам, что и боевой сервер (roots/exec_roots/
    оболочки/exec_args_unrestricted и т.д.), без дублирования этой логики здесь.
    """
    CANDIDATE_PATH.write_text(text, encoding='utf-8')
    try:
        return Policy.load(CANDIDATE_PATH)
    finally:
        CANDIDATE_PATH.unlink(missing_ok=True)


def backup_policy() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    dest = BACKUP_DIR / f'policy.toml.{stamp}.bak'
    shutil.copy2(POLICY_PATH, dest)
    return dest


_NEGATION_AFTER_RE = re.compile(r'^\w{0,4}\s+не\s+(нужн|треб|обязательн)')
_NEGATION_BEFORE_RE = re.compile(r'без\s*$')


def _keyword_active(low: str, keyword: str) -> bool:
    """`keyword` встречается в `low`, и рядом с ним нет отрицания.

    Ловит «запись НЕ нужна» / «без записи» — иначе «запись не нужна; запись
    туда отсутствует» (реальный пример из logs/requests.jsonl) предложил бы
    write_roots ровно там, где заявка просит противоположное.
    """
    idx = low.find(keyword)
    if idx == -1:
        return False
    after = low[idx + len(keyword): idx + len(keyword) + 20]
    before = low[max(0, idx - 10): idx]
    return not (_NEGATION_AFTER_RE.match(after) or _NEGATION_BEFORE_RE.search(before))


def _find_target_lists(text: str) -> set[str]:
    """Эвристика: по каким словам в тексте заявки понятно, куда просится путь.

    Порядок важен: сперва точные имена списков и характерные морфемы (запись/
    исполнение/бинарь), и только если ничего специфичного не нашлось — голое
    слово «roots» (не как часть write_roots/exec_roots) или «чтение»/read-only
    как намёк на просто `roots`. Недобор безопаснее перебора: не нашли —
    вернётся пустое множество, и build_proposal честно откажется гадать.
    """
    low = text.lower()
    hits: set[str] = set()
    if any(_keyword_active(low, kw) for kw in ('write_roots', 'запис', 'writable')):
        hits.add('write_roots')
    if any(_keyword_active(low, kw) for kw in ('exec_roots', 'исполнени', 'выполн', 'запуск', 'запусти')):
        hits.add('exec_roots')
    if any(_keyword_active(low, kw) for kw in ('exec_allowed_bin', 'бинар')):
        hits.add('exec_allowed_bin')
    if _keyword_active(low, 'exec_args_unrestricted'):
        hits.add('exec_args_unrestricted')
    if not hits and (
        re.search(r'(?<!write_)(?<!exec_)\broots\b', low)
        or any(_keyword_active(low, kw) for kw in ('чтение', 'read-only', 'видно'))
    ):
        hits.add('roots')
    return hits


def build_proposal(req: dict) -> dict | None:
    """Готовое предложение «что и куда добавить» — из заявки, а не из головы ревьюера.

    Приоритет: структурные поля request_note (path + lists) — их заполняет сам
    агент, точно зная, чего просит; заполнены — используются как есть, без
    попытки их же переспорить эвристикой. Если их нет (старая заявка или агент
    поленился) — best-effort разбор summary+detail. Возвращает None, если
    предложить нечего или итог неоднозначен (несколько разных путей в тексте,
    либо ни одного намёка на список) — тогда (a) не работает и остаётся (e).
    """
    path = str(req.get('path') or '').strip()
    raw_lists = req.get('lists')
    if path and raw_lists:
        items = raw_lists if isinstance(raw_lists, list) else str(raw_lists).split(',')
        lists = [x.strip() for x in items if str(x).strip() in EDITABLE_LISTS]
        # Поле исторически называется path, но для bin-заявки в нём лежит имя
        # бинаря. Не позволяем смешать эти два типа: абсолютный каталог никогда
        # не может оказаться в exec_allowed_bin (именно так туда попал memory_service).
        if ntpath.isabs(path):
            lists = [x for x in lists if x in PATH_LISTS]
        else:
            lists = [x for x in lists if x in BIN_LISTS]
        if lists:
            return {'path': path, 'lists': sorted(set(lists), key=EDITABLE_LISTS.index), 'source': 'structured'}

    text = f"{req.get('summary', '')} {req.get('detail', '')}"
    candidates = sorted({m.rstrip('.,;:)\'"') for m in _PATH_RE.findall(text)})
    if len(candidates) != 1:
        return None  # ноль путей — не из чего предлагать; больше одного — неоднозначно
    guessed = _find_target_lists(text)
    # _PATH_RE извлекает абсолютный путь. Слова «разрешённые бинарии» рядом с
    # ним — контекст заявки, а не просьба записать каталог в список программ.
    guessed.intersection_update(PATH_LISTS)
    if not guessed:
        return None
    return {'path': candidates[0], 'lists': sorted(guessed, key=EDITABLE_LISTS.index), 'source': 'guess'}


def _covered_by(path: str, roots: list[Path]) -> bool:
    candidate = Path(os.path.realpath(os.path.abspath(os.path.expanduser(path))))
    target = os.path.normcase(str(candidate))
    for root in roots:
        base = os.path.normcase(str(root))
        if target == base or target.startswith(base.rstrip(os.sep) + os.sep):
            return True
    return False


def _expand_path_permissions(proposal: dict) -> dict:
    """Добавить обязательные предпосылки, без которых право не исполнимо.

    `run` всегда требует writable cwd, поэтому exec_root без write_root не
    работает. А write/exec путь обязан быть виден через roots. Уже покрытые
    родительским каталогом права не дублируются.
    """
    if not ntpath.isabs(proposal['path']):
        return proposal
    policy = Policy.load(POLICY_PATH)
    lists = set(proposal['lists'])
    if 'exec_roots' in lists and not _covered_by(proposal['path'], policy.write_roots):
        lists.add('write_roots')
    if lists.intersection(('write_roots', 'exec_roots')) and not _covered_by(proposal['path'], policy.roots):
        lists.add('roots')
    return {
        **proposal,
        'lists': sorted(lists, key=EDITABLE_LISTS.index),
    }


def _apply_proposal(proposal: dict) -> tuple[bool, str]:
    """Одной транзакцией (один backup, одна проверка Policy.load) — во все списки сразу."""
    try:
        proposal = _expand_path_permissions(proposal)
    except (PolicyError, OSError) as exc:
        return False, f'текущий policy.toml не читается: {exc}'

    def mutate(text: str) -> str:
        for list_name in proposal['lists']:
            text = insert_into_list(text, list_name, proposal['path'])
        return text
    return apply_change(mutate)


def apply_change(mutate) -> tuple[bool, str]:
    """mutate(old_text) -> new_text. Валидирует, бэкапит, сохраняет.

    Возвращает (ok, message). При ok=False policy.toml НЕ тронут вообще —
    ни бэкапа, ни записи.
    """
    try:
        text = POLICY_PATH.read_text(encoding='utf-8')
    except OSError as exc:
        return False, f'policy.toml не читается: {exc}'
    try:
        candidate = mutate(text)
    except ValueError as exc:
        return False, str(exc)
    try:
        validate_candidate(candidate)
    except PolicyError as exc:
        return False, f'новый policy.toml не проходит проверку — НЕ сохранено: {exc}'
    if candidate == text:
        return True, 'это право уже было в policy.toml; очередь закрыта без дублирования'
    # backup_policy()/write_text() — реальная файловая операция на Windows: антивирус
    # может на мгновение залочить только что скопированный файл (WinError 32), диск
    # может быть занят и т.п. Раньше такой OSError улетал наверх необработанным и ронял
    # весь review_requests.py (в интерактивном режиме — вместе со ВСЕМИ решениями,
    # принятыми в этом запуске, потому что save_requests() вызывался только один раз,
    # в самом конце). Теперь это обычный (False, message) — заявка остаётся в очереди,
    # остальные уже принятые решения уже сохранены (см. save_requests в run_interactive).
    try:
        backup = backup_policy()
        POLICY_PATH.write_text(candidate, encoding='utf-8')
    except OSError as exc:
        return False, f'не удалось сохранить policy.toml: {exc}'
    return True, f'сохранено (бэкап: {backup.name}); сервер подхватит сам за несколько секунд, рестарт не нужен'


# ---------------------------------------------------------------------------
# интерактивный режим
# ---------------------------------------------------------------------------


def _prompt(msg: str) -> str:
    try:
        return input(msg).strip()
    except EOFError:
        return ''


def _interactive_approve(req: dict) -> tuple[bool, str]:
    """Ручной путь (клавиша (e)) — на случай, когда предложить нечего или оно неверное."""
    print('  Куда добавить?')
    for i, name in enumerate(EDITABLE_LISTS, 1):
        print(f'    {i}) {name}')
    task_choice = len(EDITABLE_LISTS) + 1
    print(f'    {task_choice}) новая именованная задача ([tasks.*] — безопаснее, чем открывать exec_roots)')
    choice = _prompt('  номер (пусто — отмена): ')
    if not choice:
        return False, 'отменено'
    try:
        idx = int(choice)
    except ValueError:
        return False, f'«{choice}» не число'

    if idx == task_choice:
        name = _prompt('  имя задачи (только латиница/цифры/-/_): ')
        cwd = _prompt('  cwd (абсолютный путь внутри существующего root): ')
        argv_raw = _prompt('  argv через пробел (первый токен — разрешённый бинарь): ')
        description = _prompt('  описание (можно пусто): ')
        timeout_raw = _prompt('  timeout_s (пусто — 300): ').strip() or '300'
        try:
            timeout_s = int(timeout_raw)
        except ValueError:
            return False, f'«{timeout_raw}» не число'
        argv = argv_raw.split()
        return apply_change(lambda text: append_task_block(text, name, cwd, argv, description, timeout_s))

    if not (1 <= idx <= len(EDITABLE_LISTS)):
        return False, f'{idx} вне списка'
    list_name = EDITABLE_LISTS[idx - 1]
    value = _prompt(f'  значение для {list_name}: ')
    return apply_change(lambda text: insert_into_list(text, list_name, value))


def _print_request(req: dict) -> dict | None:
    print('-' * 60)
    print(f'#{req.get("id", "?")}  [{req.get("kind", "?")}]  {req.get("ts", "?")}')
    print(f'  {req.get("summary", "")}')
    if req.get('detail'):
        print(f'  детали: {req["detail"]}')
    proposal = build_proposal(req)
    if proposal:
        hint = '' if proposal['source'] == 'structured' else '  [по тексту заявки — проверь перед (a)]'
        print(f'  предложение: {", ".join(proposal["lists"])} += \'{proposal["path"]}\'{hint}')
    else:
        print('  предложение: нет (заявка не структурирована/неоднозначна) — только (e)')
    return proposal


def run_interactive(entries: list[dict]) -> bool:
    todo = pending(entries)
    if not todo:
        print('Заявок в ожидании нет — разбирать нечего.')
        return False

    print(f'В очереди {len(todo)} заявок(и). policy.toml: {POLICY_PATH}')
    changed = False
    for req in todo:
        try:
            proposal = _print_request(req)
            menu = '  (a)одобрить / (e)вручную / (d)отклонить / (s)пропустить / (q)выйти: ' if proposal else \
                   '  (e)вручную / (d)отклонить / (s)пропустить / (q)выйти: '
            action = _prompt(menu).lower()
            if action == 'q':
                break
            if action in ('', 's'):
                continue
            if action == 'd':
                note = _prompt('  причина отказа (можно пусто): ')
                mark(req, 'denied', note)
                changed = True
                print('  -> отклонено.')
                continue
            if action == 'a' and proposal:
                ok, message = _apply_proposal(proposal)
            elif action == 'e':
                ok, message = _interactive_approve(req)
            else:
                print(f'  не понял «{action}», пропускаю.')
                continue
            if ok:
                mark(req, 'approved', message)
                changed = True
                print(f'  -> одобрено. {message}')
            else:
                print(f'  -> НЕ применено ({message}) — заявка осталась в очереди.')
        except Exception as exc:  # noqa: BLE001 — одна плохая заявка не должна ронять весь обзор
            # Раньше необработанное исключение здесь (например, OSError при записи
            # policy.toml — см. apply_change) убивало весь процесс, и ВМЕСТЕ С НИМ
            # пропадали решения по уже разобранным заявкам этого запуска: save_requests()
            # вызывался один раз, в самом конце. Теперь: (1) сохраняем очередь сразу после
            # каждого решения — потеряется максимум текущая заявка, а не весь прогресс;
            # (2) сама заявка просто остаётся pending, обзор идёт дальше.
            print(f'  -> ОШИБКА на этой заявке ({exc}) — оставляю в очереди, продолжаю.')
        finally:
            if changed:
                save_requests(entries)
    return changed


def run_auto(entries: list[dict]) -> bool:
    """--auto: только заявки со structured-предложением, без диалога.

    Осознанно НЕ трогает source=guess (эвристика по тексту заявки должна пройти
    перед глазами человека — см. build_proposal) и заявки без предложения вовсе
    (bin/network/произвольный текст — их решает review.cmd интерактивно или
    точечный --approve/--deny). Продолжает после отдельных неудач и сохраняет
    очередь после каждой заявки — тот же принцип устойчивости, что в
    run_interactive (см. комментарий там про потерю прогресса при падении).
    """
    todo = pending(entries)
    if not todo:
        print('Заявок в ожидании нет — разбирать нечего.')
        return False

    changed = False
    approved = failed = skipped = 0
    for req in todo:
        proposal = build_proposal(req)
        if not proposal or proposal.get('source') != 'structured':
            skipped += 1
            continue
        try:
            ok, message = _apply_proposal(proposal)
        except Exception as exc:  # noqa: BLE001 — см. run_interactive
            ok, message = False, str(exc)
        if ok:
            mark(req, 'approved', message)
            changed = True
            approved += 1
            print(f'#{req.get("id", "?")}: одобрено. {message}')
        else:
            failed += 1
            print(f'#{req.get("id", "?")}: НЕ применено ({message}) — осталась в очереди.')
        if changed:
            save_requests(entries)
    print(f'\nИтого: одобрено {approved}, не применено {failed}, пропущено (нет structured-предложения) {skipped}.')
    return changed


def run_triage(entries: list[dict]) -> int:
    """Разобрать всю очередь автоматом (dev_mcp.request_triage) и перенести в историю."""
    from dev_mcp import request_triage

    todo = pending(entries)
    if not todo:
        print('Заявок в ожидании нет — разбирать нечего.')
        return 0
    try:
        policy = Policy.load(POLICY_PATH)
    except (PolicyError, OSError) as exc:
        print(f'policy.toml не читается: {exc}', file=sys.stderr)
        return 1
    stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
    request_triage.triage_pending(todo, policy, stamp=stamp)
    save_requests(entries)
    for req in todo:
        print(f'#{req.get("id", "?")} [{req.get("kind", "?")}] {req["decision"]}: {str(req.get("summary", ""))[:90]}')
    print(f'\nРазобрано автоматически: {len(todo)}. Агент видит решение через request_status(id).')
    return 0


# ---------------------------------------------------------------------------
# CLI: только диагностика или автоматический дренаж
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description='Автоматический дренаж legacy request_note очереди; ручных approve/deny нет.'
    )
    p.add_argument('--list', action='store_true', help='только показать legacy-очередь и выйти')
    return p


def main(argv: list[str] | None = None) -> int:
    ns = build_arg_parser().parse_args(argv if argv is not None else sys.argv[1:])
    entries = load_requests()
    # Миграция старого формата: прежний review помечал решение, но оставлял
    # строку в активной очереди. Первый запуск новой версии перенесёт её в
    # requests.history.jsonl и физически уберёт из requests.jsonl.
    if backfill_ids(entries) or any(e.get('decision') for e in entries):
        save_requests(entries)
        entries = pending(entries)

    if ns.list:
        todo = pending(entries)
        if not todo:
            print('Заявок в ожидании нет.')
            return 0
        for req in todo:
            print(f'#{req.get("id", "?")} [{req.get("kind", "?")}] {req.get("summary", "")}'
                  + (f' — {req["detail"]}' if req.get('detail') else ''))
            proposal = build_proposal(req)
            if proposal:
                print(f'    -> {", ".join(proposal["lists"])} += \'{proposal["path"]}\'')
        return 0

    # Единственный изменяющий режим — автоматический triage. Ручной approve/deny
    # удалён из CLI: владелец не должен быть частью throughput-контура.
    return run_triage(entries)


if __name__ == '__main__':
    raise SystemExit(main())
