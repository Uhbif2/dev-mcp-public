"""Recover a proved sticky Chrome storage I/O failure, preserving its profile."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any, Callable

from dev_mcp.json_book import write_json

MIN_REPAIR_FREE_BYTES = 8 * 1024**3
EXTENSION_ID = 'ogckiochhclkelamkoidolokkaealonl'


def storage_command(*args: str) -> dict[str, Any]:
    node = shutil.which('node') or shutil.which('node.exe')
    script = Path(__file__).resolve().parents[3] / 'extension/fleet-tabs/storage-health.mjs'
    if not node or not script.is_file():
        return {'state': 'unknown', 'detail': 'storage helper missing'}
    try:
        done = subprocess.run([node, str(script), *args], capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=15,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        result = json.loads(done.stdout.strip())
        if done.returncode or not isinstance(result, dict):
            raise ValueError('storage helper failed')
        return result
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {'state': 'unknown', 'detail': str(exc)[:300]}


def probe_extension_storage() -> dict[str, Any]:
    return storage_command('--probe')


def repair_extension_storage(
    *, profile: Path | None = None, backup_root: Path | None = None,
    command: Callable[..., dict[str, Any]] = storage_command,
    disk_free: Callable[[Path], int] = lambda path: shutil.disk_usage(path).free,
    pid_finder: Callable[[], int | None] | None = None,
    main_pids: Callable[[], list[int]] | None = None,
    starter: Callable[[], int] | None = None,
    ready: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    from . import supervisor

    profile = profile or Path(os.environ.get('LOCALAPPDATA', '')) / 'FleetChrome'
    pid_finder = pid_finder or supervisor.listener_pid
    main_pids = main_pids or supervisor.fleet_chrome_main_pids
    starter = starter or supervisor.start_fleet_chrome
    ready = ready or supervisor.browser_ready
    if disk_free(profile) < MIN_REPAIR_FREE_BYTES:
        return {'state': 'waiting-space', 'detail': 'profile volume needs 8 GiB free before repair'}
    # Read-only failure/lease age cannot authorize a browser restart.
    observed = command('--probe')
    if observed.get('state') != 'io-error':
        return {'state': 'not-required' if observed.get('state') == 'ok' else 'unobserved',
                'detail': observed.get('detail', '')}
    pid = pid_finder()
    if pid is None or pid not in main_pids():
        return {'state': 'refused', 'detail': 'DevTools listener is not the dedicated FleetChrome'}
    backup_root = backup_root or Path(os.environ.get('DEV_MCP_BROWSER_RECOVERY_DIR',
        'D:/dev-mcp-recovery' if Path('D:/').is_dir() else str(profile.parent / 'FleetChrome-recovery')))
    backup_root.mkdir(parents=True, exist_ok=True)
    if backup_root.resolve().is_relative_to(profile.resolve()):
        return {'state': 'refused', 'detail': 'backup cannot be inside browser profile'}
    if disk_free(backup_root) < MIN_REPAIR_FREE_BYTES:
        return {'state': 'waiting-space', 'detail': 'backup volume needs 8 GiB free'}
    folder = backup_root / f'storage-{time.time_ns()}'
    folder.mkdir()
    exported = command('--export', str(folder / 'storage-before.json'))
    if exported.get('state') != 'exported':
        return {'state': 'backup-failed', 'detail': 'storage export failed; browser preserved'}
    before = json.loads((folder / 'storage-before.json').read_text(encoding='utf-8'))
    if not isinstance(before, dict):
        return {'state': 'backup-failed', 'detail': 'storage backup is not an object'}
    if disk_free(profile) < MIN_REPAIR_FREE_BYTES:
        return {'state': 'waiting-space', 'detail': 'profile volume lost its repair reserve'}
    closed = command('--close')
    if closed.get('state') != 'close-requested':
        return {'state': 'close-failed', 'detail': 'graceful close failed; no kill attempted'}
    close_deadline = time.monotonic() + 15
    for _ in range(30):
        if pid_finder() is None:
            break
        if time.monotonic() >= close_deadline:
            return {'state': 'close-failed', 'detail': 'graceful close budget exhausted; no kill attempted'}
        sleep(0.5)
    else:
        return {'state': 'close-failed', 'detail': 'browser did not close; no kill attempted'}
    # Offline physical backup, no deletion/replacement of the original DB or sessions.
    try:
        database = profile / 'Default/Local Extension Settings' / EXTENSION_ID
        if database.is_dir():
            shutil.copytree(database, folder / 'leveldb', symlinks=True)
    finally:
        # A backup error after close must still reopen the preserved profile.
        starter()
    verify_deadline = time.monotonic() + 30
    for _ in range(40):
        if time.monotonic() >= verify_deadline:
            return {'state': 'verification-failed', 'backup': str(folder),
                    'detail': 'storage verification budget exhausted'}
        if ready():
            checked = command('--probe')
            if checked.get('state') == 'ok':
                break
        sleep(0.5)
    else:
        return {'state': 'verification-failed', 'backup': str(folder),
                'detail': 'storage write/readback not restored'}
    after_path = folder / 'storage-after.json'
    if command('--export', str(after_path)).get('state') != 'exported':
        return {'state': 'verification-failed', 'backup': str(folder), 'detail': 'readback export failed'}
    after = json.loads(after_path.read_text(encoding='utf-8'))
    if before.get('settings') != after.get('settings'):
        return {'state': 'verification-failed', 'backup': str(folder), 'detail': 'settings differ from backup'}
    result = {'state': 'repaired', 'backup': str(folder), 'old_pid': pid,
              'new_pid': pid_finder(), 'settings_preserved': True, 'storage_write_verified': True}
    write_json(folder / 'receipt.json', result, indent=2)
    return result
