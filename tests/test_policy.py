"""Тесты границы доступа.

Это не тесты «работает ли чтение файла» — это попытки ВЫЙТИ за корень. Каждый
тест воспроизводит конкретный способ обмануть проверку пути на Windows. Если
какой-то из них покраснел, сервер нельзя выставлять наружу, пока не позеленел.

Запуск: py -m pytest tests -v   (из корня dev-mcp)
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from dev_mcp.policy import Policy, PolicyError  # noqa: E402


def make_policy(tmp_path: Path, *, allow_write: bool = True, extra: str = '') -> Policy:
    """Политика с единственным корнем внутри tmp — чтобы «наружу» было настоящим."""
    root = tmp_path / 'workspace'
    root.mkdir(exist_ok=True)
    (tmp_path / 'outside').mkdir(exist_ok=True)
    (tmp_path / 'outside' / 'secret.txt').write_text('не для агента', encoding='utf-8')

    config = tmp_path / 'policy.toml'
    config.write_text(
        'roots = [\n'
        f"    '{root}',\n"
        ']\n'
        f'allow_write = {"true" if allow_write else "false"}\n'
        "deny_patterns = ['.env', '.env.*', '*.pem', '.git', 'keys', '*secret*']\n"
        "ignore_patterns = ['node_modules']\n"
        f'{extra}',
        encoding='utf-8',
    )
    return Policy.load(config)


# ---------- выход за корень ----------


def test_traversal_dotdot_blocked(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    with pytest.raises(PolicyError, match='вне разрешённых корней'):
        policy.resolve(str(tmp_path / 'workspace' / '..' / 'outside' / 'secret.txt'), for_write=False)


def test_absolute_path_outside_blocked(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    with pytest.raises(PolicyError, match='вне разрешённых корней'):
        policy.resolve(str(tmp_path / 'outside' / 'secret.txt'), for_write=False)


def test_sibling_prefix_is_not_inside(tmp_path: Path) -> None:
    """`workspace-evil` НЕ внутри `workspace` — ловушка строкового startswith."""
    evil = tmp_path / 'workspace-evil'
    evil.mkdir()
    (evil / 'file.txt').write_text('x', encoding='utf-8')
    policy = make_policy(tmp_path)
    with pytest.raises(PolicyError, match='вне разрешённых корней'):
        policy.resolve(str(evil / 'file.txt'), for_write=False)


def test_root_itself_allowed(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    assert policy.resolve(str(tmp_path / 'workspace'), for_write=False).exists()


def test_normal_file_inside_allowed(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    target = tmp_path / 'workspace' / 'main.py'
    target.write_text('print(1)', encoding='utf-8')
    assert policy.resolve(str(target), for_write=False) == target


def test_nonexistent_file_inside_allowed_for_write(tmp_path: Path) -> None:
    """Новый файл ещё не существует — запись в корень обязана работать."""
    policy = make_policy(tmp_path)
    target = tmp_path / 'workspace' / 'sub' / 'new.txt'
    target.parent.mkdir()
    assert policy.resolve(str(target), for_write=True).name == 'new.txt'


def test_nonexistent_file_outside_blocked(tmp_path: Path) -> None:
    """Несуществующий хвост не должен становиться лазейкой наружу."""
    policy = make_policy(tmp_path)
    with pytest.raises(PolicyError, match='вне разрешённых корней'):
        policy.resolve(str(tmp_path / 'outside' / 'new.txt'), for_write=True)


# ---------- windows-специфика ----------


@pytest.mark.skipif(os.name != 'nt', reason='только Windows')
def test_unc_path_blocked(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    with pytest.raises(PolicyError, match='UNC'):
        policy.resolve(r'\\attacker\share\payload.txt', for_write=False)


@pytest.mark.skipif(os.name != 'nt', reason='только Windows')
def test_device_path_blocked(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    with pytest.raises(PolicyError, match='device'):
        policy.resolve(r'\\?\C:\Windows\System32\config\SAM', for_write=False)


@pytest.mark.skipif(os.name != 'nt', reason='только Windows')
def test_alternate_data_stream_blocked(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    target = tmp_path / 'workspace' / 'main.py'
    target.write_text('x', encoding='utf-8')
    with pytest.raises(PolicyError, match='alternate data stream'):
        policy.resolve(f'{target}:hidden', for_write=False)


@pytest.mark.skipif(os.name != 'nt', reason='только Windows')
def test_case_insensitive_root_match(tmp_path: Path) -> None:
    """C:\\USERS\\... и C:\\Users\\... — один путь; отказ по регистру был бы багом."""
    policy = make_policy(tmp_path)
    target = tmp_path / 'workspace' / 'main.py'
    target.write_text('x', encoding='utf-8')
    assert policy.resolve(str(target).upper(), for_write=False)


@pytest.mark.skipif(os.name != 'nt', reason='junction только на Windows')
def test_junction_pointing_outside_blocked(tmp_path: Path) -> None:
    """Junction внутри корня, ведущий наружу, — классический обход."""
    policy = make_policy(tmp_path)  # создаёт workspace/ и outside/ до mklink
    link = tmp_path / 'workspace' / 'escape'
    result = subprocess.run(
        ['cmd', '/c', 'mklink', '/J', str(link), str(tmp_path / 'outside')],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f'mklink недоступен: {result.stdout.strip()} {result.stderr.strip()}')
    assert link.exists(), 'junction не создался — тест ничего не проверяет'
    with pytest.raises(PolicyError, match='вне разрешённых корней'):
        policy.resolve(str(link / 'secret.txt'), for_write=False)


@pytest.mark.skipif(os.name != 'nt', reason='только Windows')
def test_short_83_name_resolved(tmp_path: Path) -> None:
    """Короткое имя 8.3 не должно прятать путь от проверки.

    На этой машине 8.3 реально в ходу (C:/MYFOLD~1/... в mcp_launcher.py). Тест
    строит короткое имя средствами ОС; если том их не генерирует — пропускаем,
    но тогда и вектора нет.
    """
    long_dir = tmp_path / 'outside' / 'очень длинное имя папки'
    long_dir.mkdir(parents=True)
    (long_dir / 'secret.txt').write_text('x', encoding='utf-8')

    # cmd `for` разбирает свою строку сам — передаём её целиком, иначе аргументы
    # разъезжаются и команда молча не даёт вывода (тест тогда «пропускается»,
    # не проверив вектор).
    result = subprocess.run(
        f'cmd /c for %I in ("{long_dir}") do @echo %~sI',
        capture_output=True,
        text=True,
        shell=True,
    )
    short = result.stdout.strip().splitlines()[-1].strip() if result.stdout.strip() else ''
    if not short or '~' not in short:
        pytest.skip('том не генерирует короткие имена 8.3')

    policy = make_policy(tmp_path)
    with pytest.raises(PolicyError, match='вне разрешённых корней'):
        policy.resolve(str(Path(short) / 'secret.txt'), for_write=False)


# ---------- deny-маски ----------


@pytest.mark.parametrize(
    'relative',
    [
        '.env',
        '.env.local',
        'sub/.env',
        'server.pem',
        'sub/keys/api.txt',
        '.git/config',
        'my_secret_stuff.txt',
    ],
)
def test_deny_patterns_block(tmp_path: Path, relative: str) -> None:
    policy = make_policy(tmp_path)
    target = tmp_path / 'workspace' / relative
    with pytest.raises(PolicyError, match='закрыт маской'):
        policy.resolve(str(target), for_write=False)


def test_deny_is_case_insensitive(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    with pytest.raises(PolicyError, match='закрыт маской'):
        policy.resolve(str(tmp_path / 'workspace' / '.ENV'), for_write=False)


def test_deny_applies_to_middle_segment(tmp_path: Path) -> None:
    """Маска ловит папку в середине пути, а не только имя файла."""
    policy = make_policy(tmp_path)
    with pytest.raises(PolicyError, match='закрыт маской'):
        policy.resolve(str(tmp_path / 'workspace' / 'proj' / '.git' / 'objects' / 'ab'), for_write=False)


def test_similar_but_allowed_name_passes(tmp_path: Path) -> None:
    """`environment.py` не должен падать под маску `.env` — иначе политика бесполезна."""
    policy = make_policy(tmp_path)
    target = tmp_path / 'workspace' / 'environment.py'
    target.write_text('x', encoding='utf-8')
    assert policy.resolve(str(target), for_write=False) == target


# ---------- обход дерева (search_files/list_dir не должны спускаться внутрь) ----------


def test_prunes_dir_matches_ignore_pattern(tmp_path: Path) -> None:
    """ignore_patterns (шум вроде node_modules) — повод не спускаться в каталог."""
    policy = make_policy(tmp_path)
    assert policy.prunes_dir('node_modules') is True


def test_prunes_dir_matches_deny_pattern(tmp_path: Path) -> None:
    """deny_patterns (секреты вроде .git/keys) — тоже повод не обходить каталог,
    а не только скрывать из выдачи постфактум."""
    policy = make_policy(tmp_path)
    assert policy.prunes_dir('.git') is True
    assert policy.prunes_dir('keys') is True


def test_prunes_dir_is_case_insensitive(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    assert policy.prunes_dir('NODE_MODULES') is True
    assert policy.prunes_dir('.GIT') is True


def test_prunes_dir_leaves_normal_names_alone(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    assert policy.prunes_dir('src') is False
    assert policy.prunes_dir('environment') is False


# ---------- режим записи ----------


def test_write_denied_in_readonly_mode(tmp_path: Path) -> None:
    policy = make_policy(tmp_path, allow_write=False)
    target = tmp_path / 'workspace' / 'main.py'
    target.write_text('x', encoding='utf-8')
    policy.resolve(str(target), for_write=False)  # чтение работает
    with pytest.raises(PolicyError, match='read-only'):
        policy.resolve(str(target), for_write=True)


# ---------- задачи ----------


def test_task_cwd_outside_root_rejected_at_load(tmp_path: Path) -> None:
    """Задача с cwd наружу — это обход границы, ловим на загрузке конфига."""
    extra = (
        '[tasks.evil]\n'
        f"cwd = '{tmp_path / 'outside'}'\n"
        "argv = ['cmd', '/c', 'dir']\n"
    )
    with pytest.raises(PolicyError, match='вне разрешённых корней'):
        make_policy(tmp_path, extra=extra)


def test_valid_task_loads(tmp_path: Path) -> None:
    extra = (
        '[tasks.build]\n'
        f"cwd = '{tmp_path / 'workspace'}'\n"
        "argv = ['cmd', '/c', 'echo', 'ok']\n"
        "description = 'тестовая задача'\n"
    )
    policy = make_policy(tmp_path, extra=extra)
    assert 'build' in policy.tasks
    assert policy.tasks['build'].argv == ['cmd', '/c', 'echo', 'ok']


def test_task_cwd_not_writable_is_marked_unrunnable(tmp_path: Path) -> None:
    """cwd задачи внутри roots, но вне write_roots: конфиг грузится, но статус
    задачи — «не выполнима». policy_info не должен молча объявлять её доступной
    (баг-репорт 2026-09-10: policy_info обещает задачу, run_task её отклоняет)."""
    root = tmp_path / 'ws'
    (root / 'proj').mkdir(parents=True)
    (root / 'box').mkdir()
    config = tmp_path / 'policy.toml'
    config.write_text(
        f"roots = ['{root}']\n"
        f"write_roots = ['{root / 'box'}']\n"
        'allow_write = false\n'
        '[tasks.checkit]\n'
        f"cwd = '{root / 'proj'}'\n"
        "argv = ['python', '-c', 'pass']\n",
        encoding='utf-8',
    )
    policy = Policy.load(config)
    runnable, reason = policy.task_status(policy.tasks['checkit'])
    assert runnable is False
    assert 'write_roots' in reason
    assert policy.describe()['tasks']['checkit']['runnable'] is False


def test_task_in_write_root_is_runnable(tmp_path: Path) -> None:
    root = tmp_path / 'ws'
    (root / 'proj').mkdir(parents=True)
    config = tmp_path / 'policy.toml'
    config.write_text(
        f"roots = ['{root}']\n"
        f"write_roots = ['{root / 'proj'}']\n"
        'allow_write = false\n'
        '[tasks.checkit]\n'
        f"cwd = '{root / 'proj'}'\n"
        "argv = ['cmd', '/c', 'echo', 'ok']\n",
        encoding='utf-8',
    )
    policy = Policy.load(config)
    assert policy.task_status(policy.tasks['checkit']) == (True, '')


# ---------- прочее ----------


def test_empty_path_rejected(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    with pytest.raises(PolicyError, match='пустой путь'):
        policy.resolve('   ', for_write=False)


def test_broken_toml_raises_policyerror_with_location(tmp_path: Path) -> None:
    """Забытая запятая в списке — самая частая ручная ошибка. Должна прийти
    PolicyError (её ловит --check / restart.py), а не голый TOMLDecodeError,
    и с указанием строки."""
    config = tmp_path / 'policy.toml'
    config.write_text(
        "roots = [\n    'C:\\\\a'\n    'C:\\\\b',\n]\n", encoding='utf-8',
    )
    with pytest.raises(PolicyError, match='TOML.*line 3'):
        Policy.load(config)


def test_missing_policy_file_raises_policyerror(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match='не читается'):
        Policy.load(tmp_path / 'нет-такого.toml')


def test_production_policy_closes_critical_names() -> None:
    """Страж боевого policy.toml.

    Тесты выше проверяют МЕХАНИЗМ на синтетической политике — он может быть
    исправен, пока в реальном конфиге забыта маска. Здесь проверяется сам
    конфиг, которым сервер поедет наружу: перечисленное обязано быть закрыто
    в каждом объявленном корне.
    """
    config = Path(__file__).resolve().parents[1] / 'policy.toml'
    policy = Policy.load(config)

    critical = [
        '.env',
        '.env.local',
        '.env.bak',
        'gateway.env',
        'id_rsa',
        'server.pem',
        'api.key',
        'keys/oracle.txt',
        '.git/config',
        '.claude/settings.json',
        'my_token_store.json',
    ]
    for root in policy.roots:
        for name in critical:
            with pytest.raises(PolicyError, match='закрыт маской'):
                policy.resolve(str(root / name), for_write=False)


def test_production_policy_has_no_global_write() -> None:
    """Глобального права записи у внешнего профиля нет — только точечные write_roots."""
    config = Path(__file__).resolve().parents[1] / 'policy.toml'
    policy = Policy.load(config)
    assert policy.allow_write is False
    assert policy.write_roots, 'ожидался хотя бы один write_root, иначе профиль полностью read-only'


def test_production_write_is_confined_to_write_roots() -> None:
    """Запись открыта ровно в объявленных папках, а корни с кодом остаются на чтение."""
    config = Path(__file__).resolve().parents[1] / 'policy.toml'
    policy = Policy.load(config)

    for write_root in policy.write_roots:
        assert policy.resolve(str(write_root / 'заметка.md'), for_write=True)

    for root in policy.roots:
        if any(str(w).startswith(str(root)) and w != root for w in policy.write_roots):
            with pytest.raises(PolicyError, match='писать нельзя|файлы корня'):
                policy.resolve(str(root / 'посторонний.txt'), for_write=True)


def test_production_task_cwd_in_write_roots() -> None:
    """Каждая задача боевого policy.toml обязана резолвить свой cwd на запись:
    run_task делает это с for_write=True и иначе отказывает, хотя policy_info
    объявляет задачу доступной (баг-репорт 2026-09-10). Guard ловит и «забыли
    внести cwd задачи в write_roots», и «убрали write_root, а задачу оставили»."""
    config = Path(__file__).resolve().parents[1] / 'policy.toml'
    policy = Policy.load(config)
    assert policy.tasks, 'ожидалась хотя бы одна задача в боевом policy.toml'
    for name, task in policy.tasks.items():
        try:
            policy.resolve(task.cwd, for_write=True)
        except PolicyError as exc:
            raise AssertionError(f'задача «{name}»: cwd вне write_roots — {exc}') from exc


# ---------- точечное право записи ----------


def make_policy_with_write_root(tmp_path: Path) -> tuple[Policy, Path, Path]:
    root = tmp_path / 'workspace'
    box = root / 'exchange'
    box.mkdir(parents=True)
    (root / 'src').mkdir()

    config = tmp_path / 'policy.toml'
    config.write_text(
        f"roots = ['{root}']\n"
        f"write_roots = ['{box}']\n"
        'allow_write = false\n'
        "deny_patterns = ['.env', '*secret*']\n",
        encoding='utf-8',
    )
    return Policy.load(config), root, box


def test_write_allowed_inside_write_root(tmp_path: Path) -> None:
    policy, _, box = make_policy_with_write_root(tmp_path)
    assert policy.resolve(str(box / 'result.md'), for_write=True)


def test_write_denied_outside_write_root(tmp_path: Path) -> None:
    """Читать в корне можно, писать — только в ящике."""
    policy, root, _ = make_policy_with_write_root(tmp_path)
    policy.resolve(str(root / 'src'), for_write=False)
    with pytest.raises(PolicyError, match='писать нельзя'):
        policy.resolve(str(root / 'src' / 'main.py'), for_write=True)


def test_deny_pattern_still_applies_inside_write_root(tmp_path: Path) -> None:
    """Точечное право записи не пробивает дырку в маске секретов."""
    policy, _, box = make_policy_with_write_root(tmp_path)
    with pytest.raises(PolicyError, match='закрыт маской'):
        policy.resolve(str(box / '.env'), for_write=True)


def test_write_root_outside_roots_rejected(tmp_path: Path) -> None:
    outside = tmp_path / 'outside'
    outside.mkdir(exist_ok=True)
    config = tmp_path / 'policy.toml'
    root = tmp_path / 'workspace'
    root.mkdir(exist_ok=True)
    config.write_text(
        f"roots = ['{root}']\nwrite_roots = ['{outside}']\nallow_write = false\n",
        encoding='utf-8',
    )
    with pytest.raises(PolicyError, match='write_root вне разрешённых корней'):
        Policy.load(config)


def test_missing_write_root_rejected(tmp_path: Path) -> None:
    root = tmp_path / 'workspace'
    root.mkdir(exist_ok=True)
    config = tmp_path / 'policy.toml'
    config.write_text(
        f"roots = ['{root}']\nwrite_roots = ['{root / 'нет-такой'}']\nallow_write = false\n",
        encoding='utf-8',
    )
    with pytest.raises(PolicyError, match='write_root .* не существует'):
        Policy.load(config)


def test_global_allow_write_still_works(tmp_path: Path) -> None:
    """Старое поведение не сломано: allow_write=true открывает запись во всех корнях."""
    policy = make_policy(tmp_path, allow_write=True)
    assert policy.resolve(str(tmp_path / 'workspace' / 'что-угодно.txt'), for_write=True)


def test_describe_does_not_leak_absolute_secrets(tmp_path: Path) -> None:
    """describe() уходит клиенту — там корни и маски, но не содержимое файлов."""
    policy = make_policy(tmp_path)
    described = policy.describe()
    assert described['roots']
    assert described['allow_write'] is True
    assert '.env' in described['deny_patterns']
