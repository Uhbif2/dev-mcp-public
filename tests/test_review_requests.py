"""Тесты tools/review_requests.py: разбор заявок без ручной правки policy.toml.

Три вещи важны здесь больше остального:
1. Текстовая вставка в список policy.toml не портит остальной файл (комментарии,
   другие списки) и сама валидируется через настоящий Policy.load.
2. Заведомо ломающая правка НЕ сохраняется вообще — ни бэкапа, ни записи.
3. Решённая заявка исчезает из очереди и остаётся доступна в history.jsonl.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'src'))

from dev_mcp.policy import Policy, PolicyError  # noqa: E402

# tools/ не пакет — грузим модуль review_requests.py по пути, как это делает review.cmd.
_SPEC = importlib.util.spec_from_file_location('review_requests', REPO / 'tools' / 'review_requests.py')
rr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rr)


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Каждый тест работает со своим policy.toml/requests.jsonl, не с боевыми."""
    monkeypatch.setattr(rr, 'POLICY_PATH', tmp_path / 'policy.toml')
    monkeypatch.setattr(rr, 'REQUESTS_PATH', tmp_path / 'requests.jsonl')
    monkeypatch.setattr(rr, 'HISTORY_PATH', tmp_path / 'requests.history.jsonl')
    monkeypatch.setattr(rr, 'CANDIDATE_PATH', tmp_path / 'policy.toml.candidate')
    monkeypatch.setattr(rr, 'BACKUP_DIR', tmp_path / 'policy_backups')


def _write_policy(tmp_path: Path, extra_root: Path | None = None) -> Path:
    root = tmp_path / 'ws'
    root.mkdir(exist_ok=True)
    (root / 'memory_service').mkdir(exist_ok=True)
    text = (
        "# комментарий, который правка не должна задеть\n"
        f"roots = [\n    '{root}',\n]\n\n"
        f"write_roots = [\n    '{root}',\n]\n\n"
        "exec_roots = [\n]\n\n"
        "exec_allowed_bin = [\n    'python',\n    'pytest',\n]\n"
    )
    cfg = tmp_path / 'policy.toml'
    cfg.write_text(text, encoding='utf-8')
    return root


# ---------- insert_into_list ----------


def test_insert_into_list_appends_and_keeps_comments(tmp_path: Path) -> None:
    root = _write_policy(tmp_path)
    text = rr.POLICY_PATH.read_text(encoding='utf-8')

    new_root = str(root / 'memory_service')
    updated = rr.insert_into_list(text, 'exec_roots', new_root)

    assert '# комментарий, который правка не должна задеть' in updated
    assert f"'{new_root}'," in updated
    # список закрывается по-прежнему одной скобкой — не задвоили вставку
    assert updated.count(f"'{new_root}'") == 1


def test_insert_into_list_rejects_quote_in_value() -> None:
    with pytest.raises(ValueError, match='одинарную кавычку'):
        rr.insert_into_list("exec_roots = [\n]\n", 'exec_roots', "C:\\it's-broken")


def test_insert_into_list_missing_list_raises() -> None:
    with pytest.raises(ValueError, match='нет списка'):
        rr.insert_into_list("roots = ['C:\\\\x']\n", 'exec_roots', 'C:\\y')


def test_insert_into_list_existing_value_is_noop(tmp_path: Path) -> None:
    root = _write_policy(tmp_path)
    text = rr.POLICY_PATH.read_text(encoding='utf-8')
    assert rr.insert_into_list(text, 'roots', str(root)) == text


# ---------- append_task_block ----------


def test_append_task_block_valid() -> None:
    text = 'roots = [\n]\n'
    updated = rr.append_task_block(text, 'memory_service-pyright', 'C:\\memory_service', ['python', '-m', 'pyright'], 'lint', 600)
    assert '[tasks.memory_service-pyright]' in updated
    assert "argv = ['python', '-m', 'pyright']" in updated
    assert 'timeout_s = 600' in updated


def test_append_task_block_rejects_bad_name() -> None:
    with pytest.raises(ValueError, match='латиница'):
        rr.append_task_block('roots = []\n', 'bad name!', 'C:\\x', ['python'])


def test_append_task_block_rejects_duplicate() -> None:
    text = "[tasks.foo]\ncwd = 'C:\\\\x'\nargv = ['python']\n"
    with pytest.raises(ValueError, match='уже есть'):
        rr.append_task_block(text, 'foo', 'C:\\x', ['python'])


# ---------- apply_change: конец-в-конец с настоящей валидацией ----------


def test_apply_change_valid_edit_is_saved_and_backed_up(tmp_path: Path) -> None:
    root = _write_policy(tmp_path)
    new_root = str(root / 'memory_service')

    ok, message = rr.apply_change(lambda text: rr.insert_into_list(text, 'exec_roots', new_root))

    assert ok is True
    assert 'рестарт не нужен' in message
    policy = Policy.load(rr.POLICY_PATH)
    assert any(str(p) == new_root for p in policy.exec_roots)
    assert list(rr.BACKUP_DIR.glob('policy.toml.*.bak'))


def test_apply_change_invalid_edit_is_not_saved(tmp_path: Path) -> None:
    _write_policy(tmp_path)
    original = rr.POLICY_PATH.read_text(encoding='utf-8')

    outside = tmp_path.parent / 'definitely-outside'
    ok, message = rr.apply_change(lambda text: rr.insert_into_list(text, 'exec_roots', str(outside)))

    assert ok is False
    assert 'НЕ сохранено' in message
    assert rr.POLICY_PATH.read_text(encoding='utf-8') == original  # ни байта не изменилось
    assert not rr.BACKUP_DIR.exists()  # бэкап не создавался — сохранять было нечего


def test_apply_change_bad_value_never_touches_policy(tmp_path: Path) -> None:
    _write_policy(tmp_path)
    original = rr.POLICY_PATH.read_text(encoding='utf-8')

    ok, message = rr.apply_change(lambda text: rr.insert_into_list(text, 'exec_roots', "bad'value"))

    assert ok is False
    assert rr.POLICY_PATH.read_text(encoding='utf-8') == original


# ---------- очередь заявок ----------


def test_pending_and_mark_roundtrip(tmp_path: Path) -> None:
    rr.REQUESTS_PATH.write_text(
        json.dumps({'id': 'aaa11122', 'kind': 'root', 'summary': 'нужен memory_service', 'ts': 'x'}) + '\n',
        encoding='utf-8',
    )
    entries = rr.load_requests()
    todo = rr.pending(entries)
    assert len(todo) == 1

    rr.mark(todo[0], 'approved', 'exec_roots += memory_service')
    rr.save_requests(entries)

    assert rr.load_requests() == []
    archived = rr.load_history()
    assert archived[0]['decision'] == 'approved'
    assert archived[0]['note'] == 'exec_roots += memory_service'


def test_find_by_id() -> None:
    entries = [{'id': 'a'}, {'id': 'b'}]
    assert rr.find_by_id(entries, 'b') is entries[1]
    assert rr.find_by_id(entries, 'missing') is None


def test_backfill_ids_only_touches_missing() -> None:
    entries = [{'id': 'keep-me'}, {'kind': 'root', 'summary': 'старая заявка без id'}]
    changed = rr.backfill_ids(entries)
    assert changed is True
    assert entries[0]['id'] == 'keep-me'
    assert entries[1]['id']  # что-то присвоено
    assert rr.backfill_ids(entries) is False  # второй проход — уже нечего менять


def test_cli_list_backfills_legacy_id_without_manual_approval(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Legacy-строка получает id, но владелец не превращается в ручной approval gate."""
    _write_policy(tmp_path)
    rr.REQUESTS_PATH.write_text(
        json.dumps({'kind': 'root', 'summary': 'легаси-заявка без id', 'ts': 'x'}) + '\n', encoding='utf-8',
    )

    assert rr.main(['--list']) == 0
    assert re.search(r'#(\w+)', capsys.readouterr().out)
    # Обычный запуск сам разбирает legacy очередь и переносит решение в history.
    assert rr.main([]) == 0
    assert rr.load_requests() == []
    assert rr.load_history()[0]['decision'] in {'approved', 'denied'}


# ---------- build_proposal: внутренние helpers старой миграции ----------


def test_build_proposal_from_structured_fields() -> None:
    req = {'path': 'C:\\Users\\dev\\projects\\EchoRedactor', 'lists': ['write_roots', 'exec_roots']}
    proposal = rr.build_proposal(req)
    assert proposal is not None
    assert proposal['source'] == 'structured'
    assert proposal['path'] == 'C:\\Users\\dev\\projects\\EchoRedactor'
    assert proposal['lists'] == ['write_roots', 'exec_roots']


def test_build_proposal_ignores_unknown_list_names() -> None:
    req = {'path': 'C:\\x', 'lists': ['write_roots', 'not_a_real_list']}
    proposal = rr.build_proposal(req)
    assert proposal['lists'] == ['write_roots']


def test_build_proposal_structured_without_valid_lists_is_none() -> None:
    assert rr.build_proposal({'path': 'C:\\x', 'lists': ['bogus']}) is None
    assert rr.build_proposal({'path': 'C:\\x', 'lists': []}) is None
    assert rr.build_proposal({'path': '', 'lists': ['roots']}) is None


def test_build_proposal_real_world_example_from_screenshot() -> None:
    """Тот самый кейс: агент уже написал путь и оба списка текстом — без path/lists
    в самой заявке (старый request_note). Эвристика обязана вытащить это сама."""
    req = {
        'kind': 'access',
        'summary': 'Разрешить запись и исполнение в EchoRedactor',
        'detail': (
            'Нужно для текущей явно разрешённой пользователем задачи: создать и запустить '
            'MVP в C:\\Users\\dev\\projects\\EchoRedactor. Добавить этот каталог '
            'в write_roots и exec_roots dev-mcp.'
        ),
    }
    proposal = rr.build_proposal(req)
    assert proposal is not None
    assert proposal['source'] == 'guess'
    assert proposal['path'] == 'C:\\Users\\dev\\projects\\EchoRedactor'
    assert proposal['lists'] == ['write_roots', 'exec_roots']


def test_absolute_path_is_never_proposed_as_binary() -> None:
    req = {
        'kind': 'root',
        'summary': 'Добавить C:\\Users\\dev\\memory_service в exec_roots',
        'detail': 'Нужны уже разрешённые бинарии uv, python и git.',
    }
    proposal = rr.build_proposal(req)
    assert proposal is not None
    assert proposal['lists'] == ['exec_roots']


def test_structured_absolute_path_drops_bin_list() -> None:
    proposal = rr.build_proposal({
        'path': 'C:\\Users\\dev\\memory_service',
        'lists': ['exec_roots', 'exec_allowed_bin'],
    })
    assert proposal is not None
    assert proposal['lists'] == ['exec_roots']


def test_build_proposal_read_only_hint_maps_to_roots_only() -> None:
    req = {
        'kind': 'root',
        'summary': 'Read-only access to C:\\Users\\dev\\.dsh\\ai-relay',
        'detail': 'Достаточно read-only; запись не нужна.',
    }
    proposal = rr.build_proposal(req)
    assert proposal is not None
    assert proposal['lists'] == ['roots']


def test_build_proposal_ambiguous_multiple_paths_returns_none() -> None:
    req = {
        'kind': 'root',
        'summary': 'Нужна запись в C:\\ops для trading autostart',
        'detail': 'Нужен write-доступ к C:\\ops\\autostart.json и запуск orchestrator.ps1.',
    }
    assert rr.build_proposal(req) is None  # два разных пути в тексте — не гадаем


def test_find_target_lists_respects_negation() -> None:
    """«запись не нужна» / «без записи» не должны предлагать write_roots."""
    assert rr._find_target_lists('запись не нужна, только чтение') == {'roots'}
    assert rr._find_target_lists('нужен доступ без записи, только read-only') == {'roots'}
    assert rr._find_target_lists('нужна запись в эту папку') == {'write_roots'}


def test_build_proposal_no_hint_returns_none() -> None:
    req = {'kind': 'other', 'summary': 'dev-mcp self-test: run_task denied by current policy', 'detail': ''}
    assert rr.build_proposal(req) is None


def test_apply_proposal_writes_to_all_listed_lists_in_one_transaction(tmp_path: Path) -> None:
    root = _write_policy(tmp_path)
    new_dir = str(root / 'memory_service')
    ok, message = rr._apply_proposal({'path': new_dir, 'lists': ['write_roots', 'exec_roots']})
    assert ok is True
    policy = Policy.load(rr.POLICY_PATH)
    assert any(str(p) == new_dir for p in policy.write_roots)
    assert any(str(p) == new_dir for p in policy.exec_roots)
    assert len(list(rr.BACKUP_DIR.glob('*.bak'))) == 1  # один backup, не два


def test_manual_approval_flags_are_removed(tmp_path: Path) -> None:
    _write_policy(tmp_path)
    for argv in (
        ['--approve', 'req0001'],
        ['--deny', 'req0001'],
        ['--interactive'],
        ['--auto'],
    ):
        with pytest.raises(SystemExit) as exc:
            rr.main(argv)
        assert exc.value.code == 2


# ---------- устойчивость: одна плохая заявка/файловая ошибка не роняет весь обзор ----------


def test_apply_change_survives_oserror_on_backup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """backup_policy()/write_text() — реальный диск: раньше OSError отсюда улетал
    необработанным и ронял весь review_requests.py (см. apply_change)."""
    root = _write_policy(tmp_path)
    original = rr.POLICY_PATH.read_text(encoding='utf-8')

    def _boom() -> Path:
        raise OSError('диск занят (симуляция)')

    monkeypatch.setattr(rr, 'backup_policy', _boom)
    ok, message = rr.apply_change(lambda text: rr.insert_into_list(text, 'exec_roots', str(root / 'memory_service')))

    assert ok is False
    assert 'не удалось сохранить' in message
    assert rr.POLICY_PATH.read_text(encoding='utf-8') == original  # файл не тронут


def test_run_interactive_saves_progress_before_a_later_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Первая заявка одобряется штатно, вторая ломается неожиданным исключением.

    Раньше save_requests() вызывался один раз, в самом конце run_interactive —
    падение на второй заявке теряло решение по первой. Теперь очередь пишется
    после каждой заявки, так что первое решение уже на диске к моменту падения.
    """
    root = _write_policy(tmp_path)
    new_dir = str(root / 'memory_service')
    rr.REQUESTS_PATH.write_text(
        json.dumps({
            'id': 'first0001', 'kind': 'access', 'summary': 'ok', 'ts': 'x',
            'path': new_dir, 'lists': ['write_roots'],
        }) + '\n'
        + json.dumps({'id': 'second002', 'kind': 'other', 'summary': 'boom', 'ts': 'x'}) + '\n',
        encoding='utf-8',
    )
    entries = rr.load_requests()

    answers = iter(['a', 'q'])  # первая заявка структурная -> (a) одобрить; до второй не дойдёт из-за исключения
    monkeypatch.setattr(rr, '_prompt', lambda _msg: next(answers))

    real_build_proposal = rr.build_proposal

    def _flaky_build_proposal(req: dict) -> dict | None:
        if req.get('id') == 'second002':
            raise RuntimeError('неожиданная ошибка на второй заявке (симуляция)')
        return real_build_proposal(req)

    monkeypatch.setattr(rr, 'build_proposal', _flaky_build_proposal)

    rr.run_interactive(entries)

    # Первое решение уже персистентно, несмотря на падение на втором элементе очереди.
    history_ids = {e['id'] for e in rr.load_history()}
    assert 'first0001' in history_ids
    pending_ids = {e['id'] for e in rr.pending(rr.load_requests())}
    assert pending_ids == {'second002'}  # осталась в очереди, не потеряна и не молча одобрена


# ---------- --auto: без диалога, только structured-предложения ----------


def test_auto_mode_approves_structured_skips_guess_and_continues_past_failure(
    tmp_path: Path,
) -> None:
    root = _write_policy(tmp_path)
    good_dir = str(root / 'memory_service')
    outside = str(tmp_path.parent / 'definitely-outside')  # provalится в Policy.load
    rr.REQUESTS_PATH.write_text(
        '\n'.join(json.dumps(e) for e in [
            {'id': 'struct001', 'kind': 'access', 'summary': 'ok', 'ts': 'x',
             'path': good_dir, 'lists': ['write_roots']},
            {'id': 'struct002', 'kind': 'access', 'summary': 'bad target', 'ts': 'x',
             'path': outside, 'lists': ['write_roots']},
            {'id': 'guess0001', 'kind': 'access', 'ts': 'x',
             'summary': 'нужна запись', 'detail': f'путь {good_dir}'},
            {'id': 'noprop001', 'kind': 'other', 'summary': 'просто заметка', 'ts': 'x'},
        ]) + '\n',
        encoding='utf-8',
    )
    entries = rr.load_requests()

    changed = rr.run_auto(entries)

    assert changed is True
    history_by_id = {e['id']: e for e in rr.load_history()}
    assert history_by_id['struct001']['decision'] == 'approved'
    assert 'struct002' not in history_by_id  # НЕ применилось -> осталась в очереди
    assert 'guess0001' not in history_by_id  # source=guess -> не трогаем без диалога
    assert 'noprop001' not in history_by_id  # нет предложения вовсе

    pending_ids = {e['id'] for e in rr.pending(rr.load_requests())}
    assert pending_ids == {'struct002', 'guess0001', 'noprop001'}


def test_cli_list_shows_only_pending(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    _write_policy(tmp_path)
    rr.REQUESTS_PATH.write_text(
        json.dumps({'id': 'p1', 'kind': 'root', 'summary': 'ждёт', 'ts': 'x'}) + '\n'
        + json.dumps({'id': 'p2', 'kind': 'root', 'summary': 'решена', 'ts': 'x', 'decision': 'approved'}) + '\n',
        encoding='utf-8',
    )
    code = rr.main(['--list'])
    assert code == 0
    out = capsys.readouterr().out
    assert 'ждёт' in out
    assert 'решена' not in out
