"""Тесты исполнения кода (`run`), скачивания (`fetch_url`) и очереди заявок.

Как и test_policy.py, это не «работает ли запуск», а попытки ВЫЙТИ за периметр:
запуститься вне exec_roots, протащить оболочку в argv[0], абсолютный путь в
аргумент, утечь секрет из окружения в дочерний процесс, скачать с loopback.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from fastmcp import Client  # noqa: E402

from dev_mcp import server as server_mod  # noqa: E402
from dev_mcp.audit import Audit  # noqa: E402
from dev_mcp.fleet.model import norm_path  # noqa: E402
from dev_mcp.fleet.store import Store  # noqa: E402
from dev_mcp.policy import Policy, PolicyError, scrub_env  # noqa: E402
from dev_mcp.server import build_server  # noqa: E402


def make_policy(tmp_path: Path, *, exec_roots: str = '', extra: str = '', network: bool = True) -> Policy:
    root = tmp_path / 'workspace'
    (root / 'proj').mkdir(parents=True, exist_ok=True)
    (root / 'code').mkdir(exist_ok=True)
    (tmp_path / 'outside').mkdir(exist_ok=True)
    config = tmp_path / 'policy.toml'
    config.write_text(
        f"roots = ['{root}']\n"
        f"write_roots = ['{root / 'proj'}', '{root / 'code'}']\n"
        + (f"exec_roots = [{exec_roots}]\n" if exec_roots else '')
        + "exec_allowed_bin = ['python', 'python3', 'py', 'pytest', 'node']\n"
        + f'allow_network_fetch = {"true" if network else "false"}\n'
        + 'allow_write = false\n'
        + "deny_patterns = ['.env', '*secret*', '.git']\n"
        + 'exec_timeout_cap_s = 30\n'
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


# ---------- scrub_env ----------


def test_scrub_env_drops_secrets_keeps_path() -> None:
    src = {
        'PATH': 'C:/bin', 'SystemRoot': 'C:/Windows', 'APPDATA': 'x',
        'OPENAI_API_KEY': 'sk-leak', 'MY_TOKEN': 'leak', 'DB_PASSWORD': 'leak',
        'SERVICE_ACCOUNT_PATH': 'C:/keys/sa.json', 'RANDOM_VAR': 'whatever',
    }
    out = scrub_env(src)
    assert out['PATH'] == 'C:/bin'
    assert 'SystemRoot' in out
    assert 'OPENAI_API_KEY' not in out
    assert 'MY_TOKEN' not in out
    assert 'DB_PASSWORD' not in out
    assert 'SERVICE_ACCOUNT_PATH' not in out  # не в passthrough
    assert 'RANDOM_VAR' not in out


# ---------- загрузка политики ----------


def test_exec_root_outside_roots_rejected(tmp_path: Path) -> None:
    outside = tmp_path / 'outside'
    with pytest.raises(PolicyError, match='exec_root вне разрешённых корней'):
        make_policy(tmp_path, exec_roots=f"'{outside}'")


@pytest.mark.parametrize('shell', ['cmd', 'powershell', 'bash', 'wscript.exe', 'pwsh'])
def test_various_shells_rejected(tmp_path: Path, shell: str) -> None:
    (tmp_path / 'workspace').mkdir(exist_ok=True)
    config = tmp_path / 'p.toml'
    config.write_text(
        f"roots = ['{tmp_path / 'workspace'}']\nexec_allowed_bin = ['python', '{shell}']\n",
        encoding='utf-8',
    )
    with pytest.raises(PolicyError, match='оболочк'):
        Policy.load(config)


# ---------- run: граница ----------


@pytest.mark.asyncio
async def test_run_denied_when_no_exec_roots(tmp_path: Path) -> None:
    server, _ = make_server(tmp_path)
    text = await call(server, 'run', {'root': str(tmp_path / 'workspace' / 'proj'), 'argv': ['python', '-c', 'print(1)']})
    assert text.startswith('Отказано')


@pytest.mark.asyncio
async def test_run_denied_outside_exec_roots_even_if_writable(tmp_path: Path) -> None:
    """code/ — write_root, но НЕ exec_root. Запись есть, исполнения нет."""
    server, _ = make_server(tmp_path, exec_roots="'{}'".format(tmp_path / 'workspace' / 'proj'))
    text = await call(server, 'run', {'root': str(tmp_path / 'workspace' / 'code'), 'argv': ['python', '-c', 'print(1)']})
    assert text.startswith('Отказано')
    assert 'exec_roots' in text


@pytest.mark.skipif(os.name != 'nt', reason='project-local batch support is Windows-specific')
@pytest.mark.asyncio
async def test_run_allows_relative_project_local_batch_and_strict_cmd_wrapper(tmp_path: Path) -> None:
    proj = tmp_path / 'workspace' / 'proj'
    proj.mkdir(parents=True, exist_ok=True)
    script = proj / 'acceptance.cmd'
    script.write_text('@echo off\r\necho batch-ok %1\r\n', encoding='utf-8')
    server, _ = make_server(tmp_path, exec_roots=f"'{proj}'")

    direct = await call(server, 'run', {'root': str(proj), 'argv': ['acceptance.cmd', 'direct']})
    assert not direct.startswith('Отказано'), direct
    assert 'batch-ok direct' in direct

    wrapped = await call(server, 'run', {
        'root': str(proj),
        'argv': ['cmd.exe', '/d', '/c', 'acceptance.cmd', 'wrapped'],
    })
    assert not wrapped.startswith('Отказано'), wrapped
    assert 'batch-ok wrapped' in wrapped

    arbitrary = await call(server, 'run', {'root': str(proj), 'argv': ['cmd.exe', '/c', 'echo', 'hi']})
    assert arbitrary.startswith('Отказано')
    assert '.cmd/.bat' in arbitrary


@pytest.mark.asyncio
async def test_run_denied_shell_binary(tmp_path: Path) -> None:
    server, _ = make_server(tmp_path, exec_roots="'{}'".format(tmp_path / 'workspace' / 'proj'))
    text = await call(server, 'run', {'root': str(tmp_path / 'workspace' / 'proj'), 'argv': ['cmd', '/c', 'echo', 'hi']})
    assert text.startswith('Отказано')
    assert 'wrapper' in text or '.cmd/.bat' in text


@pytest.mark.asyncio
async def test_run_denied_absolute_path_arg(tmp_path: Path) -> None:
    server, _ = make_server(tmp_path, exec_roots="'{}'".format(tmp_path / 'workspace' / 'proj'))
    text = await call(server, 'run', {
        'root': str(tmp_path / 'workspace' / 'proj'),
        'argv': ['python', str(tmp_path / 'outside' / 'evil.py')],
    })
    assert text.startswith('Отказано')
    assert 'абсолютный путь' in text.lower()


@pytest.mark.asyncio
async def test_run_denied_parent_escape_arg(tmp_path: Path) -> None:
    server, _ = make_server(tmp_path, exec_roots="'{}'".format(tmp_path / 'workspace' / 'proj'))
    text = await call(server, 'run', {
        'root': str(tmp_path / 'workspace' / 'proj'),
        'argv': ['python', '../../../etc/passwd'],
    })
    assert text.startswith('Отказано')


def test_absolute_arg_inside_roots_allowed(tmp_path: Path) -> None:
    """Приёмка пульта `py -3 C:/hub/_lib/x.py …`: путь внутри roots и не под deny — допустим."""
    policy = make_policy(tmp_path, exec_roots="'{}'".format(tmp_path / 'workspace' / 'proj'))
    lib = tmp_path / 'workspace' / 'code' / 'check.py'
    lib.write_text('print(1)\n', encoding='utf-8')
    policy.guard_exec_args([str(lib), 'intent/owner-brief.md'], bin_name='py')
    policy.guard_exec_args([lib.as_posix()], bin_name='py')
    secret = tmp_path / 'workspace' / 'code' / 'my-secret.txt'
    secret.write_text('x', encoding='utf-8')
    for bad in (str(secret), str(tmp_path / 'outside' / 'evil.py'),
                str(tmp_path / 'workspace' / 'code' / '..' / '..' / 'outside' / 'evil.py')):
        with pytest.raises(PolicyError):
            policy.guard_exec_args([bad], bin_name='py')


# ---------- exec_args_unrestricted ----------


def test_exec_args_unrestricted_must_be_in_allowed_bin(tmp_path: Path) -> None:
    """Освобождать от проверки аргументов можно только уже разрешённый бинарь."""
    with pytest.raises(PolicyError, match='exec_args_unrestricted'):
        make_policy(
            tmp_path,
            exec_roots="'{}'".format(tmp_path / 'workspace' / 'proj'),
            extra="exec_args_unrestricted = ['adb']\n",  # adb нет в exec_allowed_bin этого хелпера
        )


@pytest.mark.asyncio
async def test_exec_args_absolute_blocked_by_default(tmp_path: Path) -> None:
    """python НЕ в exec_args_unrestricted — абсолютный путь в аргументе отклоняется."""
    server, _ = make_server(tmp_path, exec_roots="'{}'".format(tmp_path / 'workspace' / 'proj'))
    text = await call(server, 'run', {
        'root': str(tmp_path / 'workspace' / 'proj'),
        'argv': ['python', '-c', 'print(1)', 'C:\\Windows\\System32\\drivers\\etc\\hosts'],
    })
    assert text.startswith('Отказано')
    assert 'абсолютный путь' in text.lower()


@pytest.mark.asyncio
async def test_exec_args_unrestricted_permits_absolute_and_device_paths(tmp_path: Path) -> None:
    """С python в exec_args_unrestricted тот же вызов проходит гард и исполняется."""
    server, _ = make_server(
        tmp_path,
        exec_roots="'{}'".format(tmp_path / 'workspace' / 'proj'),
        extra="exec_args_unrestricted = ['python']\n",
    )
    text = await call(server, 'run', {
        'root': str(tmp_path / 'workspace' / 'proj'),
        # аргументы в стиле adb: абсолютный хост-путь и «устройственный» /sdcard/...
        'argv': ['python', '-c', 'print("marker-ok")', 'C:\\abs\\host', '/sdcard/DCIM/x.jpg'],
    })
    assert 'marker-ok' in text
    assert 'код возврата: 0' in text


@pytest.mark.asyncio
async def test_exec_args_unrestricted_still_blocks_nul(tmp_path: Path) -> None:
    """Освобождение снимает только проверку путей — NUL в аргументе всё равно отказ."""
    server, _ = make_server(
        tmp_path,
        exec_roots="'{}'".format(tmp_path / 'workspace' / 'proj'),
        extra="exec_args_unrestricted = ['python']\n",
    )
    text = await call(server, 'run', {
        'root': str(tmp_path / 'workspace' / 'proj'),
        'argv': ['python', '-c', 'print(1)', 'bad\x00arg'],
    })
    assert text.startswith('Отказано')
    assert 'нулевой байт' in text.lower()


# ---------- run: исполнение ----------


@pytest.mark.asyncio
async def test_run_executes_and_reports_returncode(tmp_path: Path) -> None:
    server, _ = make_server(tmp_path, exec_roots="'{}'".format(tmp_path / 'workspace' / 'proj'))
    text = await call(server, 'run', {
        'root': str(tmp_path / 'workspace' / 'proj'),
        'argv': ['python', '-c', 'print("marker-42"); raise SystemExit(3)'],
    })
    assert 'marker-42' in text
    assert 'код возврата: 3' in text


@pytest.mark.asyncio
async def test_run_cwd_is_exec_root(tmp_path: Path) -> None:
    server, _ = make_server(tmp_path, exec_roots="'{}'".format(tmp_path / 'workspace' / 'proj'))
    text = await call(server, 'run', {
        'root': str(tmp_path / 'workspace' / 'proj'),
        'argv': ['python', '-c', 'import os; print(os.getcwd())'],
    })
    assert str((tmp_path / 'workspace' / 'proj').resolve()).lower() in text.lower()


@pytest.mark.asyncio
async def test_run_child_env_has_no_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('SUPER_SECRET_TOKEN', 'sk-should-not-leak')
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-should-not-leak')
    server, _ = make_server(tmp_path, exec_roots="'{}'".format(tmp_path / 'workspace' / 'proj'))
    text = await call(server, 'run', {
        'root': str(tmp_path / 'workspace' / 'proj'),
        'argv': ['python', '-c', 'import os,json; print(json.dumps(dict(os.environ)))'],
    })
    assert 'sk-should-not-leak' not in text
    assert 'SUPER_SECRET_TOKEN' not in text


@pytest.mark.asyncio
async def test_run_relative_venv_binary_allowed(tmp_path: Path) -> None:
    """argv[0] относительным путём к интерпретатору внутри exec_root — можно."""
    proj = tmp_path / 'workspace' / 'proj'
    server, _ = make_server(tmp_path, exec_roots=f"'{proj}'")
    fakebin = proj / 'tools'
    fakebin.mkdir(parents=True)
    shim = fakebin / 'python.exe'
    shim.write_bytes(Path(sys.executable).read_bytes())
    text = await call(server, 'run', {
        'root': str(proj),
        'argv': ['tools/python.exe', '-c', 'print("via-shim")'],
    })
    assert 'via-shim' in text


@pytest.mark.asyncio
async def test_run_timeout_kills(tmp_path: Path) -> None:
    server, _ = make_server(tmp_path, exec_roots="'{}'".format(tmp_path / 'workspace' / 'proj'))
    text = await call(server, 'run', {
        'root': str(tmp_path / 'workspace' / 'proj'),
        'argv': ['python', '-c', 'import time; time.sleep(30)'],
        'timeout_s': 2,
    })
    assert text.startswith('Отказано')
    assert 'таймаут' in text.lower()


@pytest.mark.asyncio
async def test_run_promotes_to_background_instead_of_dropping_the_call(tmp_path: Path) -> None:
    """Команда длиннее бюджета вызова возвращает job_id, а НЕ рвёт вызов.

    Это защита от главного способа «dev-mcp отвалился»: снаружи вызов идёт через
    шлюз, у которого свой потолок на длительность, и всё, что дольше, приходило
    агенту как 502 при живой и работающей команде.
    """
    server, _ = make_server(
        tmp_path,
        exec_roots="'{}'".format(tmp_path / 'workspace' / 'proj'),
        extra='exec_sync_budget_s = 2\n',
    )
    text = await call(server, 'run', {
        'root': str(tmp_path / 'workspace' / 'proj'),
        'argv': ['python', '-c', 'import time; print("начал", flush=True); time.sleep(20)'],
        'timeout_s': 25,
    })
    assert not text.startswith('Отказано')
    assert 'переведена в ФОН' in text
    job_id = text.split('переведена в ФОН, чтобы вызов не оборвался на шлюзе:')[1].split()[0].strip()

    # Процесс жив, его вывод не потерян, задача видна в реестре.
    status = await call(server, 'run_status', {'job_id': job_id})
    assert 'выполняется' in status
    assert 'начал' in status
    assert 'ушла в фон из синхронного run' in status

    await call(server, 'run_stop', {'job_id': job_id})


@pytest.mark.asyncio
async def test_promoted_run_still_writes_rc_to_the_fleet_journal(tmp_path: Path) -> None:
    """Уход в фон не должен ломать приёмку.

    `fleet_review accept` считает доказательством СОБСТВЕННОЕ run-событие ревьюера
    с rc=0 — а прогон тестов как раз самая длинная команда и первый кандидат на
    уход в фон. Значит rc обязан попасть в журнал и после промоушена.
    """
    import asyncio
    import sqlite3

    server, _ = make_server(
        tmp_path,
        exec_roots="'{}'".format(tmp_path / 'workspace' / 'proj'),
        extra='exec_sync_budget_s = 1\n',
    )
    text = await call(server, 'run', {
        'root': str(tmp_path / 'workspace' / 'proj'),
        'argv': ['python', '-c', 'import time; time.sleep(3); print("готово")'],
        'timeout_s': 60,
        'agent': 'W1',
    })
    assert 'переведена в ФОН' in text

    db = tmp_path / 'data' / 'fleet' / 'fleet.sqlite'
    rows: list[tuple] = []
    for _ in range(40):
        await asyncio.sleep(0.5)
        if not db.exists():
            continue
        conn = sqlite3.connect(f'file:{db.as_posix()}?mode=ro', uri=True)
        rows = list(conn.execute(
            "SELECT rc, payload FROM events WHERE kind='run' AND worker_id='W1'"))
        conn.close()
        if any(r[0] is not None for r in rows):
            break
    assert rows, 'run-событие вообще не записано'
    assert any(r[0] == 0 for r in rows), f'нет события с rc=0: {rows}'


@pytest.mark.asyncio
async def test_run_short_command_still_answers_inline(tmp_path: Path) -> None:
    """Обычный быстрый вызов уход в фон не задевает — ответ приходит сразу."""
    server, _ = make_server(
        tmp_path,
        exec_roots="'{}'".format(tmp_path / 'workspace' / 'proj'),
        extra='exec_sync_budget_s = 5\n',
    )
    text = await call(server, 'run', {
        'root': str(tmp_path / 'workspace' / 'proj'),
        'argv': ['python', '-c', 'print("быстро")'],
        'timeout_s': 20,
    })
    assert 'быстро' in text
    assert 'ФОН' not in text


@pytest.mark.asyncio
async def test_run_timeout_shorter_than_budget_still_kills(tmp_path: Path) -> None:
    """timeout_s меньше бюджета — прежнее поведение: убить и отказать."""
    server, _ = make_server(
        tmp_path,
        exec_roots="'{}'".format(tmp_path / 'workspace' / 'proj'),
        extra='exec_sync_budget_s = 30\n',
    )
    text = await call(server, 'run', {
        'root': str(tmp_path / 'workspace' / 'proj'),
        'argv': ['python', '-c', 'import time; time.sleep(30)'],
        'timeout_s': 2,
    })
    assert text.startswith('Отказано')
    assert 'таймаут' in text.lower()


@pytest.mark.asyncio
async def test_promoted_job_still_dies_at_its_own_timeout(tmp_path: Path) -> None:
    """Уход в фон продлевает жизнь ОТВЕТА, а не процесса: timeout_s остаётся в силе."""
    import asyncio

    server, _ = make_server(
        tmp_path,
        exec_roots="'{}'".format(tmp_path / 'workspace' / 'proj'),
        extra='exec_sync_budget_s = 1\n',
    )
    text = await call(server, 'run', {
        'root': str(tmp_path / 'workspace' / 'proj'),
        'argv': ['python', '-c', 'import time; time.sleep(60)'],
        'timeout_s': 3,
    })
    job_id = text.split('переведена в ФОН, чтобы вызов не оборвался на шлюзе:')[1].split()[0].strip()
    for _ in range(40):
        status = await call(server, 'run_status', {'job_id': job_id})
        if 'выполняется' not in status:
            break
        await asyncio.sleep(0.5)
    assert 'убита по таймауту' in status


@pytest.mark.asyncio
async def test_run_output_clipped(tmp_path: Path) -> None:
    server, _ = make_server(tmp_path, exec_roots="'{}'".format(tmp_path / 'workspace' / 'proj'))
    text = await call(server, 'run', {
        'root': str(tmp_path / 'workspace' / 'proj'),
        'argv': ['python', '-c', 'print("x" * 40000)'],
    })
    assert 'обрезано' in text


# ---------- run: фон ----------


@pytest.mark.asyncio
async def test_run_background_lifecycle(tmp_path: Path) -> None:
    server, _ = make_server(tmp_path, exec_roots="'{}'".format(tmp_path / 'workspace' / 'proj'))
    started = await call(server, 'run', {
        'root': str(tmp_path / 'workspace' / 'proj'),
        'argv': ['python', '-c', 'import time; [print(i) or time.sleep(0.3) for i in range(100)]'],
        'background': True,
    })
    assert 'фоновая задача' in started
    job_id = started.split('фоновая задача:')[1].split()[0].strip()

    status = await call(server, 'run_status', {'job_id': job_id})
    assert 'выполняется' in status

    stopped = await call(server, 'run_stop', {'job_id': job_id})
    assert job_id in stopped

    listing = await call(server, 'run_list', {})
    assert job_id in listing


@pytest.mark.asyncio
@pytest.mark.parametrize('test_run', [True, False])
async def test_background_pytest_deadline_is_passed_to_registry(tmp_path, monkeypatch, test_run):
    from types import SimpleNamespace
    from dev_mcp.runner import JobRegistry
    observed=[]
    def fake_start(self, argv, cwd, env, *, timeout_s=None):
        observed.append(timeout_s)
        return SimpleNamespace(job_id='abcdef123456')
    monkeypatch.setattr(JobRegistry,'start',fake_start)
    monkeypatch.setattr(JobRegistry,'when_done',lambda *args,**kwargs:None)
    root=tmp_path/'workspace'/'proj'
    server,_=make_server(tmp_path,exec_roots=f"'{root}'")
    (root/'server.py').write_text('')
    argv=['python','-m','pytest'] if test_run else ['python','server.py']
    reply=await call(server,'run',{'root':str(root),'argv':argv,'background':True,'timeout_s':2})
    assert 'фоновая задача' in reply
    assert observed==[2 if test_run else None]


@pytest.mark.asyncio
async def test_run_status_recovers_promoted_result_from_fleet_journal(tmp_path: Path) -> None:
    server, _ = make_server(tmp_path, exec_roots="'{}'".format(tmp_path / 'workspace' / 'proj'))
    job_id = 'abcdef123456'
    store = Store.open(str(tmp_path / 'data' / 'fleet' / 'fleet.sqlite'))
    with store.tx() as tx:
        tx.record_event(
            kind='run',
            worker_id='W1',
            rc=0,
            payload={'argv': ['python', '-m', 'pytest'], 'promoted': job_id},
        )

    status = await call(server, 'run_status', {'job_id': job_id})
    assert not status.startswith('Отказано')
    assert 'код возврата 0' in status
    assert 'восстановлен из журнала флота' in status
    assert 'Не запускай команду заново' in status


@pytest.mark.asyncio
async def test_run_status_recovers_background_log_after_server_restart(tmp_path: Path) -> None:
    server, _ = make_server(tmp_path, exec_roots="'{}'".format(tmp_path / 'workspace' / 'proj'))
    job_id = '123456abcdef'
    runs = tmp_path / 'logs' / 'runs'
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f'{job_id}.log').write_text('# old background job\nuseful-result\n', encoding='utf-8')

    status = await call(server, 'run_status', {'job_id': job_id})
    assert not status.startswith('Отказано')
    assert 'предыдущей сессии dev-mcp' in status
    assert 'финальный rc неизвестен' in status
    assert 'useful-result' in status


# ---------- fetch_url ----------


@pytest.mark.asyncio
async def test_fetch_denied_when_disabled(tmp_path: Path) -> None:
    server, _ = make_server(tmp_path, network=False)
    text = await call(server, 'fetch_url', {'url': 'https://example.com/x', 'dest': str(tmp_path / 'workspace' / 'proj' / 'x')})
    assert text.startswith('Отказано')


@pytest.mark.asyncio
async def test_fetch_denied_non_http_scheme(tmp_path: Path) -> None:
    server, _ = make_server(tmp_path)
    text = await call(server, 'fetch_url', {'url': 'file:///C:/Windows/win.ini', 'dest': str(tmp_path / 'workspace' / 'proj' / 'x')})
    assert text.startswith('Отказано')
    assert 'http' in text


@pytest.mark.asyncio
@pytest.mark.parametrize('url', [
    'http://127.0.0.1:8765/servers/dev/mcp',
    'http://localhost:3001/mcp',
    'http://169.254.169.254/latest/meta-data/',
    'http://10.0.0.5/x',
    'http://[::1]/x',
])
async def test_fetch_denied_private_hosts(tmp_path: Path, url: str) -> None:
    server, _ = make_server(tmp_path)
    text = await call(server, 'fetch_url', {'url': url, 'dest': str(tmp_path / 'workspace' / 'proj' / 'x')})
    assert text.startswith('Отказано')
    assert 'адрес отклонён' in text


@pytest.mark.asyncio
async def test_fetch_denied_dest_outside_write_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_mod, '_is_public_host', lambda h: (True, ''))
    server, _ = make_server(tmp_path)
    text = await call(server, 'fetch_url', {'url': 'https://example.com/x', 'dest': str(tmp_path / 'workspace' / 'code' / '..' / 'nope.txt')})
    # code/../nope.txt == workspace/nope.txt — не write_root
    assert text.startswith('Отказано')


@pytest.mark.asyncio
async def test_fetch_happy_path_and_size_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import http.server
    import threading

    payload = b'A' * 5000

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):  # тише
            pass

    httpd = http.server.HTTPServer(('127.0.0.1', 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    monkeypatch.setattr(server_mod, '_is_public_host', lambda h: (True, ''))

    server, _ = make_server(tmp_path, extra='fetch_max_bytes = 10000\n')
    dest = tmp_path / 'workspace' / 'proj' / 'dl.bin'
    text = await call(server, 'fetch_url', {'url': f'http://127.0.0.1:{port}/f', 'dest': str(dest)})
    assert 'скачано 5000' in text
    assert dest.read_bytes() == payload

    server2, _ = make_server(tmp_path, extra='fetch_max_bytes = 100\n')
    text2 = await call(server2, 'fetch_url', {'url': f'http://127.0.0.1:{port}/f', 'dest': str(tmp_path / 'workspace' / 'proj' / 'big.bin'), 'overwrite': True})
    assert text2.startswith('Отказано')
    assert 'лимит' in text2
    httpd.shutdown()


# ---------- request_note ----------


@pytest.mark.asyncio
async def test_request_note_is_triaged_immediately_and_persisted_to_history(tmp_path: Path) -> None:
    server, _ = make_server(tmp_path)
    r1 = await call(server, 'request_note', {'kind': 'bin', 'summary': 'нужен docker', 'detail': 'для compose'})
    assert 'разобрана автоматически' in r1 and 'denied' in r1
    r2 = await call(server, 'request_note', {'kind': 'weird-kind', 'summary': 'ещё одна'})
    assert '(other)' in r2  # неизвестный kind нормализован

    reqs = (tmp_path / 'logs' / 'requests.history.jsonl').read_text(encoding='utf-8').strip().splitlines()
    assert len(reqs) == 2
    assert json.loads(reqs[0])['kind'] == 'bin'
    assert json.loads(reqs[0])['decision'] == 'denied'

    listing = await call(server, 'request_list', {})
    assert 'заявок нет' in listing.casefold()


@pytest.mark.asyncio
async def test_request_note_rejects_empty_summary(tmp_path: Path) -> None:
    server, _ = make_server(tmp_path)
    text = await call(server, 'request_note', {'kind': 'access', 'summary': '   '})
    assert text.startswith('Отказано')


@pytest.mark.asyncio
async def test_request_note_stores_structured_path_and_lists(tmp_path: Path) -> None:
    """Агент, точно знающий путь и список(-ки), передаёт их структурно — тогда
    review_requests.py сможет одобрить заявку одним нажатием, без ручного ввода."""
    server, _ = make_server(tmp_path)
    await call(server, 'request_note', {
        'kind': 'access', 'summary': 'нужен доступ',
        'path': 'C:\\Users\\dev\\projects\\EchoRedactor',
        'lists': ['write_roots', 'exec_roots'],
    })
    entry = json.loads((tmp_path / 'logs' / 'requests.history.jsonl').read_text(encoding='utf-8').strip())
    assert entry['path'] == 'C:\\Users\\dev\\projects\\EchoRedactor'
    assert entry['lists'] == ['write_roots', 'exec_roots']


@pytest.mark.asyncio
async def test_request_note_without_path_omits_structured_fields(tmp_path: Path) -> None:
    server, _ = make_server(tmp_path)
    await call(server, 'request_note', {'kind': 'other', 'summary': 'просто заметка'})
    entry = json.loads((tmp_path / 'logs' / 'requests.history.jsonl').read_text(encoding='utf-8').strip())
    assert 'path' not in entry
    assert 'lists' not in entry


@pytest.mark.asyncio
async def test_request_note_returns_id_and_status_is_immediate(tmp_path: Path) -> None:
    """request_note сразу решает legacy access/root заявку; pending owner-очереди нет."""
    server, _ = make_server(tmp_path)
    note = await call(server, 'request_note', {'kind': 'root', 'summary': 'нужен memory_service'})
    match = re.search(r'Заявка\s+(\w+)', note)
    assert match, note
    req_id = match.group(1)

    status = await call(server, 'request_status', {'id': req_id})
    assert 'denied' in status

    listing = await call(server, 'request_list', {})
    assert req_id not in listing


@pytest.mark.asyncio
async def test_request_status_unknown_id_denied(tmp_path: Path) -> None:
    server, _ = make_server(tmp_path)
    text = await call(server, 'request_status', {'id': 'nope'})
    assert text.startswith('Отказано')


@pytest.mark.asyncio
async def test_request_status_finds_request_routed_into_fleet(tmp_path: Path) -> None:
    server, _ = make_server(tmp_path)
    store = Store.open(str(tmp_path / 'data' / 'fleet' / 'fleet.sqlite'))
    project_root = tmp_path / 'workspace' / 'proj'
    owned = norm_path(str(project_root / 'owned.txt'))
    stamp = '2026-09-25T00:00:00+00:00'
    with store.tx() as tx:
        tx.change(
            'INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)',
            ('demo', str(project_root)),
            event={'kind': 'seed'},
        )
        tx.change(
            """INSERT INTO tasks(
                   project,tz,title,state,priority,kind,spec_path,spec_sha,
                   needs,owns,resources,acceptance,author_worker,body,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                'demo', 'TZ-1', 'Target', 'ready', 1, 'work', 'ToDo/TZ-1.md', 'sha',
                '[]', json.dumps([owned]), '[]', '[]', None, 'Target body', stamp, stamp,
            ),
            event={'kind': 'seed'},
        )

    note = await call(server, 'request_note', {
        'kind': 'other',
        'summary': 'поправить соседнюю зону',
        'to': 'demo/TZ-1',
    })
    match = re.search(r'id=([0-9a-f]+)', note)
    assert match, note
    req_id = match.group(1)

    status = await call(server, 'request_status', {'id': req_id})
    assert f'{req_id}: fleet task demo/request:{req_id}' in status
    assert 'ready' in status
    assert 'ответа владельца не жди' in status


@pytest.mark.asyncio
async def test_request_status_reads_immediate_history_decision(tmp_path: Path) -> None:
    server, _ = make_server(tmp_path)
    note = await call(server, 'request_note', {'kind': 'root', 'summary': 'нужен memory_service'})
    req_id = re.search(r'Заявка\s+(\w+)', note).group(1)

    status = await call(server, 'request_status', {'id': req_id})
    assert 'denied' in status
    assert 'владельца' not in status.casefold() or 'вручную' not in status.casefold()
    listing = await call(server, 'request_list', {})
    assert req_id not in listing


def test_server_start_drains_legacy_pending_requests(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    logs = tmp_path / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    pending = {
        'id': 'legacy01',
        'ts': '2026-09-25T00:00:00+00:00',
        'kind': 'root',
        'summary': 'старый запрос прав',
    }
    (logs / 'requests.jsonl').write_text(json.dumps(pending, ensure_ascii=False) + '\n', encoding='utf-8')
    audit = Audit(logs / 'audit.jsonl')

    build_server(policy, audit)

    assert (logs / 'requests.jsonl').read_text(encoding='utf-8') == ''
    archived = [json.loads(line) for line in (logs / 'requests.history.jsonl').read_text(encoding='utf-8').splitlines()]
    assert archived[-1]['id'] == 'legacy01'
    assert archived[-1]['decision'] in {'approved', 'denied'}
    assert archived[-1]['auto'] is True


@pytest.mark.asyncio
async def test_policy_reload_picks_up_edit_without_restart(tmp_path: Path) -> None:
    """Ядро всей задачи: агент правит/видит policy.toml через отдельный процесс
    (человек + review_requests.py), а РАБОТАЮЩИЙ сервер подхватывает это по
    вызову policy_reload — без пересоздания build_server/перезапуска процесса."""
    policy = make_policy(tmp_path)
    audit = Audit(tmp_path / 'logs' / 'audit.jsonl')
    policy_path = tmp_path / 'policy.toml'
    server = build_server(policy, audit, policy_path=policy_path)

    before = await call(server, 'policy_info', {})
    assert 'exec' in before.lower()

    new_exec = tmp_path / 'workspace' / 'proj'
    text = policy_path.read_text(encoding='utf-8')
    text += f"\nexec_roots = ['{new_exec}']\n"
    policy_path.write_text(text, encoding='utf-8')

    result = await call(server, 'policy_reload', {})
    assert 'перечитан' in result
    assert str(new_exec) in [str(p) for p in policy.exec_roots]

    run_result = await call(server, 'run', {'root': str(new_exec), 'argv': ['python', '-c', 'print(1)']})
    assert not run_result.startswith('Отказано'), run_result


@pytest.mark.asyncio
async def test_policy_reload_without_path_configured_fails_clean(tmp_path: Path) -> None:
    server, _ = make_server(tmp_path)  # без policy_path
    result = await call(server, 'policy_reload', {})
    assert result.startswith('Отказано')


# ---------- боевой policy.toml ----------


@pytest.mark.production
def test_production_exec_roots_inside_roots_and_no_shell() -> None:
    config = Path(__file__).resolve().parents[1] / 'policy.toml'
    policy = Policy.load(config)
    for er in policy.exec_roots:
        assert policy._owning_root(er) is not None, f'{er} вне roots'
    from dev_mcp.policy import _SHELL_BIN_BLOCKLIST
    for b in policy.exec_allowed_bin:
        assert b.lower() not in _SHELL_BIN_BLOCKLIST


@pytest.mark.production
def test_production_policy_has_no_duplicate_permissions_or_directory_as_binary() -> None:
    """review.cmd не должен плодить дубли и путать путь проекта с argv[0]."""
    config = Path(__file__).resolve().parents[1] / 'policy.toml'
    policy = Policy.load(config)
    for values in (policy.roots, policy.write_roots, policy.exec_roots):
        normalized = [str(value).casefold() for value in values]
        assert len(normalized) == len(set(normalized))
    for name in policy.exec_allowed_bin:
        assert Path(name).name == name, f'каталог/путь ошибочно попал в exec_allowed_bin: {name}'


@pytest.mark.production
def test_production_allowed_bins_resolve_in_launch_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """restart.cmd/run.cmd добавляют эти стабильные каталоги в PATH сервиса."""
    stable_dirs = (
        Path.home() / 'memory_service' / '.venv' / 'Scripts',
        Path.home() / 'platform-tools',
    )
    launch_path = os.pathsep.join((os.environ.get('PATH', ''), *(str(path) for path in stable_dirs)))
    monkeypatch.setenv('PATH', launch_path)

    config = Path(__file__).resolve().parents[1] / 'policy.toml'
    policy = Policy.load(config)
    assert policy._exec_bin_missing == []


@pytest.mark.production
def test_production_memory_service_has_complete_agent_permissions() -> None:
    config = Path(__file__).resolve().parents[1] / 'policy.toml'
    policy = Policy.load(config)
    memory_service = str(Path(r'C:\Users\dev\memory_service').resolve()).casefold()
    assert memory_service in {str(path).casefold() for path in policy.roots}
    assert memory_service in {str(path).casefold() for path in policy.write_roots}
    assert memory_service in {str(path).casefold() for path in policy.exec_roots}
    assert {'python', 'uv', 'git'} <= {name.casefold() for name in policy.exec_allowed_bin}
    assert {'memory_service-lint', 'memory_service-typecheck', 'memory_service-test'} <= set(policy.tasks)


@pytest.mark.production
def test_production_every_exec_root_is_write_root() -> None:
    """Каждый exec_root обязан быть и write_root: процесс `run` пишет кэш/pyc в
    cwd, а cwd проходит проверку for_write. Иначе `run` там молча не заработает."""
    config = Path(__file__).resolve().parents[1] / 'policy.toml'
    policy = Policy.load(config)
    write = {str(p) for p in policy.write_roots}
    for er in policy.exec_roots:
        assert str(er) in write, f'{er} есть в exec_roots, но не в write_roots'


@pytest.mark.production
def test_production_exec_args_unrestricted_subset_of_allowed_bin() -> None:
    config = Path(__file__).resolve().parents[1] / 'policy.toml'
    policy = Policy.load(config)
    stems = {(b[:-4] if b.lower().endswith('.exe') else b).lower() for b in policy.exec_allowed_bin}
    for name in policy.exec_args_unrestricted:
        assert name.lower() in stems, f'{name} в exec_args_unrestricted, но не в exec_allowed_bin'


# ---------- cwd относительно root ----------


def test_relative_cwd_is_resolved_against_root_not_server_cwd(tmp_path: Path) -> None:
    # 23.09.2026: cwd='.' уходил в рабочую папку процесса dev-mcp → «сюда писать нельзя».
    proj = tmp_path / 'workspace' / 'proj'
    policy = make_policy(tmp_path, exec_roots=f"'{proj}'")
    (proj / 'sub').mkdir(exist_ok=True)
    assert policy.resolve_exec_cwd(str(proj), '.') == proj.resolve()
    assert policy.resolve_exec_cwd(str(proj), 'sub') == (proj / 'sub').resolve()
    with pytest.raises(PolicyError):
        policy.resolve_exec_cwd(str(proj), '..')  # выход из exec_root по-прежнему закрыт


@pytest.mark.asyncio
async def test_run_status_waits_for_running_job_instead_of_returning_at_once(tmp_path: Path, monkeypatch) -> None:
    """Опрос фоновой команды сам ждёт её завершения: агенту не нужно «проверить позже»."""
    import time as _time

    monkeypatch.setattr(server_mod, 'RUN_STATUS_WAIT_S', 10.0)
    server, _ = make_server(
        tmp_path,
        exec_roots="'{}'".format(tmp_path / 'workspace' / 'proj'),
        extra='exec_sync_budget_s = 1\n',
    )
    text = await call(server, 'run', {
        'root': str(tmp_path / 'workspace' / 'proj'),
        'argv': ['python', '-c', 'import time; time.sleep(3); print("готово", flush=True)'],
        'timeout_s': 20,
    })
    job_id = text.split('переведена в ФОН, чтобы вызов не оборвался на шлюзе:')[1].split()[0].strip()
    t0 = _time.monotonic()
    status = await call(server, 'run_status', {'job_id': job_id})
    assert _time.monotonic() - t0 < 9
    assert 'код возврата 0' in status
    assert 'готово' in status

    monkeypatch.setattr(server_mod, 'RUN_STATUS_WAIT_S', 2.0)
    text = await call(server, 'run', {
        'root': str(tmp_path / 'workspace' / 'proj'),
        'argv': ['python', '-c', 'import time; time.sleep(15)'],
        'timeout_s': 20,
    })
    job_id = text.split('переведена в ФОН, чтобы вызов не оборвался на шлюзе:')[1].split()[0].strip()
    status = await call(server, 'run_status', {'job_id': job_id})
    assert 'выполняется' in status and 'Сразу снова вызови' in status
    await call(server, 'run_stop', {'job_id': job_id})


def test_project_local_bin_is_not_missing_and_bare_name_hints_relative_path(tmp_path: Path) -> None:
    """sing-box.exe живёт в build/ проекта: «нет в PATH» для него не поломка (18.10.2026)."""
    config = tmp_path / 'policy.toml'
    config.write_text(
        f"roots = ['{tmp_path.as_posix()}']\nexec_roots = ['{tmp_path.as_posix()}']\n"
        "exec_allowed_bin = ['python', 'no-such-tool-zz.exe']\n"
        "exec_bin_project_local = ['no-such-tool-zz.exe']\n", encoding='utf-8')
    policy = Policy.load(config)
    assert 'no-such-tool-zz.exe' not in policy._exec_bin_missing
    with pytest.raises(PolicyError, match='относительным путём'):
        policy.exec_bin_path('no-such-tool-zz', tmp_path)
    config.write_text(config.read_text(encoding='utf-8').replace(
        "exec_bin_project_local = ['no-such-tool-zz.exe']", "exec_bin_project_local = ['ghost.exe']"),
        encoding='utf-8')
    with pytest.raises(PolicyError, match='exec_bin_project_local'):
        Policy.load(config)
