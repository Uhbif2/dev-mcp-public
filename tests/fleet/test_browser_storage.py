import json
from pathlib import Path

import pytest

from dev_mcp.fleet.browser_storage import repair_extension_storage


def setup_repair(tmp_path, *, state='io-error', free=10 * 1024**3, export_ok=True, probe_after='ok'):
    profile = tmp_path / 'FleetChrome'
    profile.mkdir(parents=True)
    effects = []
    live = [42]
    calls = [0]

    def command(*args):
        effects.append(args[0])
        if args[0] == '--probe':
            calls[0] += 1
            return {'state': state if calls[0] == 1 else probe_after}
        if args[0] == '--export':
            if not export_ok:
                return {'state': 'unknown'}
            Path(args[1]).write_text(json.dumps({'settings': {'enabled': True}, 'sessions': {'a': 1}}))
            return {'state': 'exported'}
        if args[0] == '--close':
            live.clear()
            return {'state': 'close-requested'}

    def start():
        effects.append('start')
        live.append(43)
        return 43

    kwargs = dict(profile=profile, backup_root=tmp_path / 'backup', command=command,
                  disk_free=lambda _: free, pid_finder=lambda: live[0] if live else None,
                  main_pids=lambda: [42], starter=start, ready=lambda: bool(live), sleep=lambda _: None)
    return kwargs, effects


@pytest.mark.parametrize('state', ['ok', 'unknown'])
def test_unconfirmed_failure_never_closes_browser(tmp_path, state):
    kwargs, effects = setup_repair(tmp_path, state=state)
    result = repair_extension_storage(**kwargs)
    assert result['state'] in {'not-required', 'unobserved'}
    assert '--close' not in effects


def test_no_space_waits_without_restart_or_deleting_data(tmp_path):
    kwargs, effects = setup_repair(tmp_path, free=100)
    assert repair_extension_storage(**kwargs)['state'] == 'waiting-space'
    assert effects == []


def test_failed_backup_or_wrong_browser_cannot_authorize_close(tmp_path):
    kwargs, effects = setup_repair(tmp_path, export_ok=False)
    assert repair_extension_storage(**kwargs)['state'] == 'backup-failed'
    assert '--close' not in effects
    kwargs, effects = setup_repair(tmp_path / 'other', export_ok=False)
    kwargs['main_pids'] = lambda: [99]
    assert repair_extension_storage(**kwargs)['state'] == 'refused'
    assert '--close' not in effects


def test_verified_sticky_failure_reopens_saved_profile_and_checks_persistence(tmp_path):
    kwargs, effects = setup_repair(tmp_path)
    result = repair_extension_storage(**kwargs)
    assert result['state'] == 'repaired'
    assert result['settings_preserved'] and result['storage_write_verified']
    assert result['old_pid'] == 42 and result['new_pid'] == 43
    assert effects == ['--probe', '--export', '--close', 'start', '--probe', '--export']
    assert (Path(result['backup']) / 'storage-before.json').is_file()


def test_restart_request_without_successful_storage_readback_is_not_repair(tmp_path):
    kwargs, effects = setup_repair(tmp_path, probe_after='io-error')
    assert repair_extension_storage(**kwargs)['state'] == 'verification-failed'
    assert effects.count('--close') == 1 and effects.count('start') == 1
