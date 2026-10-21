"""TZ-32: an accepted change must be self-contained at HEAD, not just in the dirty tree."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from dev_mcp.fleet.integrate import Integrator  # noqa: E402
from dev_mcp.fleet.store import Store  # noqa: E402


def git(root: Path, *args: str) -> str:
    return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True,
                          text=True, encoding='utf-8').stdout


@pytest.fixture
def ctx(tmp_path: Path):
    root = tmp_path / 'project'
    root.mkdir()
    git(root, 'init', '-q')
    git(root, 'config', 'user.name', 'Test')
    git(root, 'config', 'user.email', 'test@example.invalid')
    package = root / 'src' / 'pkg'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('', encoding='utf-8')
    (package / 'shared.py').write_text('result = 1\n', encoding='utf-8')
    git(root, 'add', '-A')
    git(root, 'commit', '-q', '-m', 'baseline')
    store = Store.open(str(tmp_path / 'fleet.sqlite'))
    with store.tx() as tx:
        tx.change('INSERT INTO projects(name,root,priority,active) VALUES(?,?,1,1)',
                  ('P', str(root)), event={'kind': 'setup'})
    integ = Integrator(store, tmp_path / 'cursor.json',
                       project_info=lambda _: {'commit': True})
    assert integ.tick() == []
    return root, store, integ


def task(store: Store, tz: str, paths: list[Path], state: str = 'taken') -> None:
    stamp = datetime.now(timezone.utc).isoformat()
    owns = json.dumps([str(p).replace('\\', '/').casefold() for p in paths])
    with store.tx() as tx:
        tx.change('INSERT INTO tasks(project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,'
                  'resources,acceptance,author_worker,body,created_at,updated_at) '
                  'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  ('P', tz, tz, state, 5, 'work', '', '', '[]', owns, '[]', '[]', None,
                   '', stamp, stamp), event={'kind': 'setup'})


def emit(store: Store, kind: str, tz: str, path: Path | None = None) -> None:
    with store.tx() as tx:
        tx.record_event(kind=kind, project='P', tz=tz, worker_id='W1',
                        path_norm=str(path).replace('\\', '/').casefold() if path else '')


def accept(store: Store, tz: str) -> None:
    with store.tx() as tx:
        tx.change("UPDATE tasks SET state='accepted' WHERE project='P' AND tz=?", (tz,),
                  event={'kind': 'test_accept'})
    emit(store, 'accept', tz)


def integration(out: list[dict], tz: str) -> dict:
    return next(e['payload'] for e in out if e['kind'] == 'integrate' and e['tz'] == tz)


def test_defers_foreign_unfinished_write_then_retries_after_supplier_accepted(ctx) -> None:
    root, store, integ = ctx
    shared = root / 'src' / 'pkg' / 'shared.py'
    supplier = root / 'src' / 'pkg' / 'supplier.py'
    shared.write_text('from pkg.supplier import result\n', encoding='utf-8')
    supplier.write_text('result = 42\n', encoding='utf-8')
    task(store, 'TZ-A', [shared])
    task(store, 'TZ-B', [supplier])
    emit(store, 'write', 'TZ-B', shared)
    accept(store, 'TZ-A')

    deferred = integration(integ.tick(), 'TZ-A')
    assert deferred['deferred'] and deferred['waiting_for'] == ['TZ-B']
    assert deferred['obstacle']['type'] == 'needs_result'
    assert git(root, 'show', 'HEAD:src/pkg/shared.py') == 'result = 1\n'
    assert not git(root, 'ls-tree', '-r', '--name-only', 'HEAD').count('supplier.py')

    accept(store, 'TZ-B')
    subsequent = integ.tick()
    assert integration(subsequent, 'TZ-B')['commit']
    # Supplier owned the module and wrote the shared file, so its acceptance may
    # integrate both paths atomically; the earlier acceptance is then a no-op.
    assert integration(subsequent, 'TZ-A').get('commit') or integration(subsequent, 'TZ-A').get('skipped')
    assert git(root, 'show', 'HEAD:src/pkg/shared.py') == 'from pkg.supplier import result\n'
    assert git(root, 'show', 'HEAD:src/pkg/supplier.py') == 'result = 42\n'
    assert integ.tick() == []


def test_dirty_tree_green_but_missing_module_in_commit_candidate_defers(ctx) -> None:
    root, store, integ = ctx
    shared = root / 'src' / 'pkg' / 'shared.py'
    shared.write_text('from pkg.uncommitted import item\n', encoding='utf-8')
    (shared.parent / 'uncommitted.py').write_text('item = 1\n', encoding='utf-8')
    task(store, 'TZ-A', [shared])
    accept(store, 'TZ-A')
    event = integration(integ.tick(), 'TZ-A')
    assert event['deferred'] and 'ModuleNotFoundError' in event['reason']
    assert git(root, 'show', 'HEAD:src/pkg/shared.py') == 'result = 1\n'


def test_normal_commit_and_unrelated_file_excluded(ctx) -> None:
    root, store, integ = ctx
    changed = root / 'src' / 'pkg' / 'shared.py'
    changed.write_text('result = 2\n', encoding='utf-8')
    (root / 'stranger.py').write_text('x = 1\n', encoding='utf-8')
    task(store, 'TZ-A', [changed])
    accept(store, 'TZ-A')
    event = integration(integ.tick(), 'TZ-A')
    assert event['commit']
    assert git(root, 'show', 'HEAD:src/pkg/shared.py') == 'result = 2\n'
    assert 'stranger.py' not in git(root, 'ls-tree', '-r', '--name-only', 'HEAD')


def test_deletion_rename_and_unicode_paths_preserved(ctx) -> None:
    root, store, integ = ctx
    old = root / 'src' / 'pkg' / 'shared.py'
    renamed = root / 'src' / 'pkg' / 'новый.py'
    old.rename(renamed)
    renamed.write_text('result = 12\n', encoding='utf-8')
    task(store, 'TZ-A', [old, renamed])
    accept(store, 'TZ-A')
    assert integration(integ.tick(), 'TZ-A')['commit']
    names = git(root, '-c', 'core.quotepath=off', 'ls-tree', '-r', '--name-only', 'HEAD')
    assert 'src/pkg/новый.py' in names
    assert 'src/pkg/shared.py' not in names


def test_python_syntax_failure_blocks_commit(ctx) -> None:
    root, store, integ = ctx
    changed = root / 'src' / 'pkg' / 'shared.py'
    changed.write_text('def = broken\n', encoding='utf-8')
    task(store, 'TZ-A', [changed])
    accept(store, 'TZ-A')
    event = integration(integ.tick(), 'TZ-A')
    assert event['deferred'] and 'py_compile:' in event['reason']
    assert git(root, 'show', 'HEAD:src/pkg/shared.py') == 'result = 1\n'


def test_deleting_module_is_blocked_when_committed_importer_still_needs_it(ctx) -> None:
    root, store, integ = ctx
    shared = root / 'src' / 'pkg' / 'shared.py'
    consumer = root / 'src' / 'pkg' / 'consumer.py'
    consumer.write_text('from pkg.shared import result\n', encoding='utf-8')
    git(root, 'add', '-A')
    git(root, 'commit', '-q', '-m', 'consumer')
    shared.unlink()
    task(store, 'TZ-A', [shared])
    accept(store, 'TZ-A')
    event = integration(integ.tick(), 'TZ-A')
    assert event['deferred'] and 'ModuleNotFoundError' in event['reason']
    assert 'src/pkg/shared.py' in git(root, 'ls-tree', '-r', '--name-only', 'HEAD')


@pytest.mark.parametrize(('importer_path', 'import_line'), [
    ('src/pkg/consumer.py', 'from .shared import result\n'),
    ('src/pkg/consumer.py', 'from . import shared\n'),
    ('src/pkg/sub/consumer.py', 'from ..shared import result\n'),
])
def test_deleting_module_blocks_relative_importers(
    ctx, importer_path: str, import_line: str,
) -> None:
    root, store, integ = ctx
    shared = root / 'src' / 'pkg' / 'shared.py'
    importer = root / importer_path
    importer.parent.mkdir(parents=True, exist_ok=True)
    if importer.parent != shared.parent:
        (importer.parent / '__init__.py').write_text('', encoding='utf-8')
    importer.write_text(import_line, encoding='utf-8')
    git(root, 'add', '-A')
    git(root, 'commit', '-q', '-m', 'relative importer')
    shared.unlink()

    task(store, 'TZ-A', [shared])
    accept(store, 'TZ-A')
    event = integration(integ.tick(), 'TZ-A')
    assert event['deferred'] and event['reason'].startswith('import: ')
    assert 'shared' in event['reason']
    assert 'src/pkg/shared.py' in git(root, 'ls-tree', '-r', '--name-only', 'HEAD')


def test_project_with_pytest_gate_collects_tests_from_candidate(ctx) -> None:
    root, store, _ = ctx
    # Committed test references a module that exists only in the dirty tree.
    (root / 'test_candidate.py').write_text('from pkg.missing import item\n', encoding='utf-8')
    git(root, 'add', '-A')
    git(root, 'commit', '-q', '-m', 'test dependency')
    (root / 'src' / 'pkg' / 'missing.py').write_text('item = 2\n', encoding='utf-8')
    integ = Integrator(store, root.parent / 'cursor.json',
                       project_info=lambda _: {'commit': True, 'gate': 'pytest -q'})
    changed = root / 'src' / 'pkg' / 'shared.py'
    changed.write_text('result = 3\n', encoding='utf-8')
    task(store, 'TZ-A', [changed])
    accept(store, 'TZ-A')
    event = integration(integ.tick(), 'TZ-A')
    assert event['deferred'] and 'pytest collect:' in event['reason']
    assert git(root, 'show', 'HEAD:src/pkg/shared.py') == 'result = 1\n'


def test_deferred_obstacle_is_linked_and_resolved_for_mapped_outcome(ctx) -> None:
    from dev_mcp.fleet import outcome

    root, store, integ = ctx
    shared = root / 'src' / 'pkg' / 'shared.py'
    supplier = root / 'src' / 'pkg' / 'supplier.py'
    shared.write_text('from pkg.supplier import result\n', encoding='utf-8')
    supplier.write_text('result = 42\n', encoding='utf-8')
    task(store, 'TZ-A', [shared])
    task(store, 'TZ-B', [supplier])
    result = outcome.create_outcome(
        store, project='P', title='Integrate A', intent_id='integration-A',
        spec={'goal': 'committed'}, evidence_spec={'gate': 'green'}, authority={'fleet': True},
    )
    outcome.link_work(store, outcome_id=result.id, project='P', tz='TZ-A')
    emit(store, 'write', 'TZ-B', shared)
    accept(store, 'TZ-A')
    assert integration(integ.tick(), 'TZ-A')['deferred']
    with store.read() as tx:
        opened = tx.query("SELECT type,change_source,state FROM obstacles WHERE outcome_id=?", (result.id,))
    assert len(opened) == 1 and opened[0]['type'] == 'needs_result'
    assert opened[0]['change_source'] == 'P/TZ-B' and opened[0]['state'] == 'open'

    accept(store, 'TZ-B')
    integ.tick()
    with store.read() as tx:
        state = tx.scalar('SELECT state FROM obstacles WHERE outcome_id=?', (result.id,))
    assert state == 'resolved'


def test_candidate_script_dir_with_sibling_import_is_not_imported_as_package(ctx) -> None:
    """17.10: workloads/baseline/collect.py `from receipt import ...` deferred forever."""
    root, _, integ = ctx
    scripts = root / 'workloads' / 'baseline'
    scripts.mkdir(parents=True)
    (scripts / 'receipt.py').write_text('def validate():\n    return 1\n', encoding='utf-8')
    (scripts / 'collect.py').write_text('from receipt import validate\n', encoding='utf-8')
    ok, reason = integ._candidate(str(root), ['workloads/baseline/collect.py',
                                              'workloads/baseline/receipt.py'], 'P')
    assert ok, reason


def test_candidate_script_dir_still_compiles(ctx) -> None:
    root, _, integ = ctx
    scripts = root / 'workloads'
    scripts.mkdir()
    (scripts / 'broken.py').write_text('def x(:\n', encoding='utf-8')
    ok, reason = integ._candidate(str(root), ['workloads/broken.py'], 'P')
    assert not ok and reason.startswith('py_compile: ')


def test_candidate_sees_untracked_server_intent_checker(ctx) -> None:
    """17.10: acceptance `py -3 .intent/check.py` failed in candidate — .intent is untracked."""
    root, _, integ = ctx
    (root / '.intent').mkdir()
    (root / '.intent' / 'check.py').write_text('import sys\nsys.exit(0)\n', encoding='utf-8')
    (root / 'notes.md').write_text('x\n', encoding='utf-8')
    ok, reason = integ._candidate(str(root), ['notes.md'], 'P',
                                  [f'"{sys.executable}" .intent/check.py'])
    assert ok, reason
    assert 'check.py' not in git(root, 'ls-files')


def test_failed_candidate_opens_one_repair_request_for_author(ctx) -> None:
    """17.10: accept с провалом импорта/приёмки ретраился сутками без решателя."""
    root, store, integ = ctx
    shared = root / 'src' / 'pkg' / 'shared.py'
    consumer = root / 'src' / 'pkg' / 'consumer.py'
    consumer.write_text('from pkg.shared import result\n', encoding='utf-8')
    git(root, 'add', '-A')
    git(root, 'commit', '-q', '-m', 'consumer')
    shared.unlink()
    task(store, 'TZ-A', [shared])
    accept(store, 'TZ-A')
    assert integration(integ.tick(), 'TZ-A')['deferred']
    integ.tick()
    with store.read() as tx:
        rows = tx.query("SELECT tz,body FROM tasks WHERE project='P' AND tz LIKE 'request:%'")
    assert len(rows) == 1, [r['tz'] for r in rows]


def test_async_gate_holds_only_its_project_and_accept_survives(ctx) -> None:
    """17.10: tick ждал гейт Beta App до 87 мин — стояла интеграция всех проектов."""
    import threading
    root, store, integ = ctx
    integ.async_gates = True
    release = threading.Event()
    busy = threading.Thread(target=release.wait, daemon=True)
    busy.start()
    integ._gate_threads['P'] = busy
    new = root / 'src' / 'pkg' / 'new.py'
    new.write_text('value = 2\n', encoding='utf-8')
    task(store, 'TZ-G', [new])
    accept(store, 'TZ-G')
    held = integration(integ.tick(), 'TZ-G')
    assert held.get('gate_wait') and 'commit' not in held
    assert 'src/pkg/new.py' not in git(root, 'ls-tree', '-r', '--name-only', 'HEAD')
    release.set()
    busy.join(5)
    done = integration(integ.tick(), 'TZ-G')
    assert done.get('commit'), done
    assert 'src/pkg/new.py' in git(root, 'ls-tree', '-r', '--name-only', 'HEAD')


def test_async_gate_runs_in_background_and_records_event(ctx) -> None:
    root, store, integ = ctx
    integ.async_gates = True
    integ.project_info = lambda _: {'commit': True,
                                    'gate': f'"{sys.executable}" -c "import time; time.sleep(1)"'}
    new = root / 'src' / 'pkg' / 'bg.py'
    new.write_text('value = 3\n', encoding='utf-8')
    task(store, 'TZ-B', [new])
    accept(store, 'TZ-B')
    out = integ.tick()
    assert integration(out, 'TZ-B').get('commit')
    assert not any(e['kind'] == 'gate' for e in out)  # tick не ждал гейт
    integ._gate_threads['P'].join(60)
    with store.read() as tx:
        assert tx.one("SELECT rc FROM events WHERE kind='gate' AND project='P'")['rc'] == 0


def test_candidate_node_modules_link_is_removed_without_touching_target(tmp_path) -> None:
    """17.10: node acceptance needs the project's node_modules; cleanup must not follow the link."""
    from dev_mcp.fleet.integrate import _link_dir, _unlink_dir
    modules = tmp_path / 'project' / 'node_modules' / 'pkg'
    modules.mkdir(parents=True)
    (modules / 'index.js').write_text('module.exports = 1', encoding='utf-8')
    link = _link_dir(modules.parent, tmp_path / 'candidate-node_modules')
    assert (link / 'pkg' / 'index.js').is_file()
    _unlink_dir(link)
    assert not link.exists()
    assert (modules / 'index.js').is_file()


def test_run_command_resolves_windows_batch_shim(tmp_path, monkeypatch) -> None:
    """17.10: `npm test` failed in the candidate — npm is npm.cmd, not found without PATHEXT."""
    import os
    from dev_mcp.fleet import exec_preflight
    if os.name != 'nt':
        return
    shim = tmp_path / 'fakenpm.cmd'
    shim.write_text('@echo shim-ok %*\r\n', encoding='utf-8')
    monkeypatch.setenv('PATH', str(tmp_path) + os.pathsep + os.environ.get('PATH', ''))
    result = exec_preflight.run_command('fakenpm test', cwd=tmp_path)
    assert result.ok, (result.returncode, result.stderr)
    assert 'shim-ok test' in result.stdout
