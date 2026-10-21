"""Регрессии на «сервер тормозит и виснет, когда работают сразу десять агентов».

Каждый тест здесь закрывает КОНКРЕТНЫЙ механизм, который к этому приводил, а не
скорость как таковую: скорость зависит от машины и диска, механизм — нет.

Механизмов было три, и все три дают одну и ту же картину снаружи — «вызовы
тормозят, а то и виснут», хотя причины разные:

1. `resolve()` на каждый файл обхода. realpath + exists + маски по каждому
   сегменту — 0.75 мс на файл, то есть ~50 с чистого питона на большом корне.
   Это время идёт под GIL, поэтому десять одновременных поисков отбирают
   интерпретатор у ВСЕХ остальных вызовов: `policy_info` отвечал 600 мс вместо
   17 мс. Стережёт `test_scan_does_not_resolve_every_file`.
2. `run` с `stdout=PIPE`: `communicate()` ждёт закрытия трубы, а трубу
   наследуют внуки (демон adb, dev-server). Вызов висел весь таймаут, а после
   `taskkill` уходил в `communicate()` без таймаута — то есть навсегда, вместе
   с потоком пула. Стережёт `test_run_returns_even_if_grandchild_holds_output`.
3. Отсутствие потолка на одновременные тяжёлые вызовы: пул потоков, в котором
   FastMCP выполняет синхронные инструменты, конечен, и лёгкому вызову просто
   не оставалось потока. Стережёт `test_gate_refuses_instead_of_queueing`.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from fastmcp import Client  # noqa: E402

from dev_mcp.audit import Audit  # noqa: E402
from dev_mcp.policy import Policy, PolicyError, ScanTimeout  # noqa: E402
from dev_mcp.server import _Gate, build_server  # noqa: E402


def make_policy(tmp_path: Path, *, exec_roots: str = '', extra: str = '') -> Policy:
    root = tmp_path / 'workspace'
    (root / 'pkg').mkdir(parents=True, exist_ok=True)
    config = tmp_path / 'policy.toml'
    config.write_text(
        f"roots = ['{root}']\n"
        f"write_roots = ['{root}']\n"
        + (f'exec_roots = [{exec_roots}]\n' if exec_roots else '')
        + "exec_allowed_bin = ['python', 'python3', 'py']\n"
        + 'exec_timeout_cap_s = 60\n'
        + "deny_patterns = ['.env', '*secret*', '.git']\n"
        + "ignore_patterns = ['__pycache__', 'node_modules']\n"
        + extra,
        encoding='utf-8',
    )
    return Policy.load(config)


def make_server(tmp_path: Path, **kw):
    policy = make_policy(tmp_path, **kw)
    audit = Audit(tmp_path / 'logs' / 'audit.jsonl')
    return build_server(policy, audit), policy


async def call(server, name: str, args: dict) -> str:
    async with Client(server) as client:
        result = await client.call_tool(name, args)
        return result.content[0].text


# ---------- 1. обход не платит resolve() за каждый файл ----------


def test_scan_does_not_resolve_every_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Главная регрессия: ни одного resolve() на обычных файлах дерева.

    Именно этот вызов (realpath + exists + маски по сегментам) стоил 0.75 мс на
    файл и превращал поиск по большому корню в минуту работы под GIL.
    """
    policy = make_policy(tmp_path)
    root = tmp_path / 'workspace'
    for i in range(20):
        (root / 'pkg' / f'f{i}.py').write_text('x', encoding='utf-8')

    seen: list[str] = []
    original = Policy.resolve

    def counting(self, raw_path: str, *, for_write: bool):
        seen.append(raw_path)
        return original(self, raw_path, for_write=for_write)

    monkeypatch.setattr(Policy, 'resolve', counting)
    items = list(policy.scan(root))

    assert len(items) == 20
    assert seen == [], f'обход всё ещё зовёт resolve(): {seen[:3]}'


def test_scan_prunes_ignored_and_denied_subtrees(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    root = tmp_path / 'workspace'
    (root / 'pkg' / '__pycache__').mkdir(parents=True, exist_ok=True)
    (root / 'pkg' / '__pycache__' / 'cached.py').write_text('x', encoding='utf-8')
    (root / '.git').mkdir(exist_ok=True)
    (root / '.git' / 'config').write_text('x', encoding='utf-8')
    (root / 'pkg' / 'real.py').write_text('x', encoding='utf-8')
    (root / '.env').write_text('SECRET=1', encoding='utf-8')

    names = {item.name for item in policy.scan(root)}
    assert names == {'real.py'}


def test_scan_stops_on_deadline(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    root = tmp_path / 'workspace'
    (root / 'pkg' / 'a.py').write_text('x', encoding='utf-8')

    with pytest.raises(ScanTimeout):
        list(policy.scan(root, deadline=time.monotonic() - 1))


def test_scan_does_not_follow_directory_link_outside_root(tmp_path: Path) -> None:
    """Симлинк/junction — единственный способ увести обход за корень.

    Раньше границу держал resolve() на каждой находке; теперь — то, что внутрь
    reparse point обход не спускается вовсе. Проверяем именно это.
    """
    policy = make_policy(tmp_path)
    root = tmp_path / 'workspace'
    outside = tmp_path / 'outside'
    outside.mkdir(exist_ok=True)
    (outside / 'private.txt').write_text('чужое', encoding='utf-8')
    try:
        os.symlink(outside, root / 'link', target_is_directory=True)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip('создание симлинков в этой системе недоступно')

    found = [str(item.path) for item in policy.scan(root)]
    assert not any('private.txt' in path for path in found), found


# ---------- 2. поиск не читает двоичные файлы ----------


@pytest.mark.asyncio
async def test_search_skips_binary_files(tmp_path: Path) -> None:
    """Двоичные не читаются — как в grep. Иначе на реальном корне весь бюджет
    времени уходил на вендоренные SDK, и до настоящих исходников поиск не
    доходил вовсе."""
    server, _ = make_server(tmp_path)
    root = tmp_path / 'workspace'
    (root / 'pkg' / 'note.txt').write_text('маркер-поиска здесь', encoding='utf-8')
    (root / 'pkg' / 'blob.bin').write_bytes(b'\x00\x01' + 'маркер-поиска'.encode())
    (root / 'pkg' / 'lib.so').write_text('маркер-поиска', encoding='utf-8')

    text = await call(server, 'search_files', {'query': 'маркер-поиска'})
    assert 'note.txt' in text
    assert 'blob.bin' not in text  # NUL в начале
    assert 'lib.so' not in text    # по расширению, даже не открывая


@pytest.mark.asyncio
async def test_search_regex_with_line_anchor_still_matches(tmp_path: Path) -> None:
    """Предфильтр по всему тексту не должен глотать построчные якоря `^`/`$`."""
    server, _ = make_server(tmp_path)
    (tmp_path / 'workspace' / 'pkg' / 'code.py').write_text(
        'import os\ndef собери():\n    pass\n', encoding='utf-8'
    )
    text = await call(server, 'search_files', {'query': r'^def собери', 'regex': True})
    assert 'code.py' in text


# ---------- 3. run возвращается, даже если вывод держит внук ----------


@pytest.mark.asyncio
async def test_run_returns_even_if_grandchild_holds_output(tmp_path: Path) -> None:
    """Команда отработала за миг, но оставила фоновый процесс с тем же stdout.

    Так ведёт себя adb (поднимает демон), dev-server, инсталлятор службы. С
    `stdout=PIPE` вызов висел до самого таймаута, а потом — в `communicate()`
    без таймаута, то есть навсегда. Ждать тут нечего: сам процесс завершился.
    """
    proj = tmp_path / 'workspace' / 'pkg'
    server, _ = make_server(tmp_path, exec_roots=f"'{proj}'")
    child_code = (
        'import subprocess, sys; '
        'subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]); '
        'print("родитель отработал")'
    )

    started = time.monotonic()
    text = await call(server, 'run', {
        'root': str(proj), 'argv': ['python', '-c', child_code], 'timeout_s': 25,
    })
    elapsed = time.monotonic() - started

    assert 'родитель отработал' in text
    assert 'код возврата: 0' in text
    assert elapsed < 15, f'вызов ждал внука {elapsed:.1f} с вместо того, чтобы вернуться сразу'


@pytest.mark.asyncio
async def test_run_timeout_keeps_output_printed_before_kill(tmp_path: Path) -> None:
    """При таймауте отдаём то, что процесс успел напечатать.

    С трубой этот вывод терялся целиком: в журнале такие отказы стоят с пустым
    выводом, и по ним нельзя было понять, на чём именно команда встала.
    """
    proj = tmp_path / 'workspace' / 'pkg'
    server, _ = make_server(tmp_path, exec_roots=f"'{proj}'")
    child_code = (
        'import sys, time; sys.stdout.write("дошли до сюда\\n"); '
        'sys.stdout.flush(); time.sleep(60)'
    )

    text = await call(server, 'run', {
        'root': str(proj), 'argv': ['python', '-c', child_code], 'timeout_s': 2,
    })

    assert 'превышен таймаут' in text
    assert 'дошли до сюда' in text


@pytest.mark.asyncio
async def test_run_leaves_no_log_files_behind(tmp_path: Path) -> None:
    proj = tmp_path / 'workspace' / 'pkg'
    server, _ = make_server(tmp_path, exec_roots=f"'{proj}'")
    await call(server, 'run', {'root': str(proj), 'argv': ['python', '-c', 'print(1)']})

    runs_dir = tmp_path / 'logs' / 'runs'
    assert list(runs_dir.glob('sync-*.log')) == []


# ---------- 4. шлюз отказывает честно, а не копит очередь ----------


def test_gate_refuses_instead_of_queueing() -> None:
    """Занятый шлюз отвечает отказом за отведённое время.

    Молчаливая очередь здесь хуже отказа: на том конце агент, который по своему
    таймауту всё равно перезапросит, а вызов к тому моменту уже никому не нужен —
    но поток пула держит.
    """
    gate = _Gate(1, 'тестовых вызовов', wait_s=0.1)
    held = threading.Event()
    release = threading.Event()

    def occupy() -> None:
        with gate.hold():
            held.set()
            release.wait(5)

    worker = threading.Thread(target=occupy, daemon=True)
    worker.start()
    assert held.wait(5)

    with pytest.raises(PolicyError, match='занят'):
        with gate.hold():
            pass

    release.set()
    worker.join(5)
    # Шлюз освободился — следующий вызов проходит.
    with gate.hold() as inflight:
        assert inflight == 1


def test_gate_counts_inflight() -> None:
    gate = _Gate(2, 'тестовых вызовов', wait_s=0.1)
    with gate.hold() as first:
        assert first == 1
        with gate.hold() as second:
            assert second == 2
    assert gate.inflight == 0


def test_decode_output_mixed_encodings() -> None:
    """Заголовок сервера в utf-8, вывод процесса в cp1251 или cp866 — всё читаемо."""
    from dev_mcp.runner import decode_output

    raw = (
        '# python путь\n'.encode('utf-8')
        + 'дошли до сюда\r\n'.encode('cp1251')
        + 'Отказано в доступе\r\n'.encode('cp866')
    )
    text = decode_output(raw)
    assert '# python путь' in text
    assert 'дошли до сюда' in text
    assert 'Отказано в доступе' in text


def test_sync_budget_counts_from_call_start_not_process_start() -> None:
    import time as _time

    from dev_mcp.server import _budget_left

    t0 = _time.monotonic() - 20  # 20 с ушло на ожидание слота
    assert _budget_left(35, t0) in (14, 15)
    assert _budget_left(35, _time.monotonic() - 60) == 5  # меньше 5 с не даём: процесс должен успеть стартовать


def test_agent_reads_fleet_console_through_run_but_cannot_change_it() -> None:
    from dev_mcp.server import _pult_read

    assert _pult_read(["fleet.cmd", "result", "Beta App/TZ-383"]) == ["result", "Beta App/TZ-383"]
    assert _pult_read(["py", "-3", "tools/fleet.py", "active"]) == ["active"]
    assert "только на чтение" in _pult_read(["fleet.cmd", "freeze", "P/TZ-1"])
    assert _pult_read(["py", "-3", "-m", "pytest"]) is None


def test_denied_binary_says_which_tool_to_use(tmp_path) -> None:
    import pytest as _pytest

    from dev_mcp.policy import _TOOL_HINTS

    assert "search_files" in _TOOL_HINTS["rg"]
    assert "read_file" in _TOOL_HINTS["cat"]
