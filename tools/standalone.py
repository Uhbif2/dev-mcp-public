"""Set up and operate a standalone loopback dev-mcp installation.

All configuration belongs to this checkout. No registry, scheduled task or
machine-wide network setting is changed. Remote access uses an independently
authorized Secure MCP Tunnel or an authenticated gateway.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def autostart(root: Path, manifest: Path, port: int, *, remove: bool = False):
    """Opt-in integration with the owner's existing manifest-based launcher."""
    before = manifest.read_bytes()
    document = json.loads(before.decode('utf-8-sig'))
    if not isinstance(document.get('items'), list):
        raise ValueError('expected an autostart manifest with an items array')
    name = 'dev-mcp-' + hashlib.sha256(str(root).casefold().encode()).hexdigest()[:12]
    command = [str(root / 'tools/standalone.py'), 'start', '--port', str(port)]
    entry = {'name': name, 'description': f'Standalone dev-mcp: {root}',
             'managedBy': 'orchestrator', 'enabled': True,
             'check': {'type': 'cmdline', 'value': str(root / 'policy.toml')},
             'start': {'exe': str(root / '.venv/Scripts/python.exe'),
                       'args': subprocess.list2cmdline(command), 'workdir': str(root), 'hidden': True},
             'postCheckTimeoutSec': 60}
    previous = next((x for x in document['items'] if x.get('name') == name), None)
    if previous and previous.get('check') != entry['check']:
        raise ValueError('autostart entry was changed; refusing to overwrite it')
    document['items'] = [x for x in document['items'] if x.get('name') != name]
    if not remove:
        document['items'].append(entry)
    backup = root / 'data' / f'autostart-before-{time.time_ns()}.json'
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(before)
    if manifest.read_bytes() != before:
        raise RuntimeError('autostart manifest changed concurrently; retry after reviewing it')
    manifest.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
    if json.loads(manifest.read_text(encoding='utf-8')) != document:
        raise RuntimeError('autostart readback failed')
    return {'autostart': not remove, 'entry': name, 'backup': str(backup)}


def jsonrpc(port: int, method: str, params: dict, session: str = '', *, timeout: float = 3):
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}
    if session:
        headers['Mcp-Session-Id'] = session
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}).encode()
    req = urllib.request.Request(f'http://127.0.0.1:{port}/servers/dev/mcp', data=body, headers=headers)
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout) as res:
        raw = res.read(2 * 1024 * 1024)
        data = b'\n'.join(line[5:].strip() for line in raw.splitlines() if line.startswith(b'data:')) or raw
        return json.loads(data), res.headers.get('Mcp-Session-Id', session)


def probe(port: int) -> dict:
    data, sid = jsonrpc(port, 'initialize', {'protocolVersion': '2025-06-18',
        'capabilities': {}, 'clientInfo': {'name': 'standalone-doctor', 'version': '1'}})
    if data.get('result', {}).get('serverInfo', {}).get('name') != 'dev-mcp':
        raise RuntimeError('the endpoint is not dev-mcp')
    result, _ = jsonrpc(port, 'tools/call', {'name': 'policy_info', 'arguments': {}}, sid)
    if result.get('error') or result.get('result', {}).get('isError'):
        raise RuntimeError('policy_info failed')
    return {'initialize': True, 'policy_info': True}


def init(root: Path, workspace: Path, name: str) -> None:
    if not workspace.is_dir():
        raise ValueError('workspace must already exist')
    targets = [root / x for x in ('policy.toml', 'machine.toml', 'fleet-projects.toml')]
    if any(p.exists() for p in targets):
        raise ValueError('configuration already exists; init never overwrites it')
    value = json.dumps(str(workspace.resolve()).replace('\\', '/'), ensure_ascii=False)
    policy = f'''roots = [{value}]
write_roots = [{value}]
allow_write = false
deny_patterns = [".git", ".env", ".env.*", "*secret*", "*token*", "*.pem", "*.key"]
allow_network_fetch = false

exec_roots = [{value}]
exec_allowed_bin = ["python", "py", "git"]
exec_timeout_cap_s = 600
'''
    sys.path.insert(0, str(root / 'src'))
    from dev_mcp.policy import Policy
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        trial = Path(tmp, 'policy.toml')
        trial.write_text(policy, encoding='utf-8')
        Policy.load(str(trial))
    targets[0].write_text(policy, encoding='utf-8')
    targets[1].write_text(f'workspaces = [{value}]\nhub = {json.dumps(str(root.parent).replace(chr(92), "/"))}\n', encoding='utf-8')
    targets[2].write_text(f'[projects.{json.dumps(name, ensure_ascii=False)}]\n'
                          'goal = "Work only inside the registered workspace."\ncommit = false\n', encoding='utf-8')
    from dev_mcp.fleet.store import Store
    store = Store.open(str(root / 'data/fleet/fleet.sqlite'))
    try:
        with store.tx() as tx:
            tx.change('INSERT INTO projects(name,root,active) VALUES(?,?,1)',
                      (name, str(workspace.resolve())), event='standalone_project_created')
    finally:
        store.close()


def identity(root: Path):
    import psutil
    path = root / 'data/standalone-process.json'
    if not path.exists():
        return None
    record = json.loads(path.read_text(encoding='utf-8'))
    try:
        process = psutil.Process(record['pid'])
        command = process.cmdline()
        if (abs(process.create_time() - record['created']) > 0.01
                or '-m' not in command or 'dev_mcp' not in command
                or str(root / 'policy.toml') not in command):
            raise RuntimeError('process identity changed; refusing to control it')
        return process, record
    except psutil.NoSuchProcess:
        return None


def start(root: Path, port: int, core_only: bool) -> dict:
    existing = identity(root)
    if existing:
        return {'running': True, 'pid': existing[0].pid, **probe(existing[1]['port'])}
    python = root / ('.venv/Scripts/python.exe' if os.name == 'nt' else '.venv/bin/python')
    if not python.is_file():
        raise ValueError('create .venv and install this project first')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', port))  # Refuse an occupied port before launching anything.
    (root / 'logs').mkdir(exist_ok=True)
    (root / 'data').mkdir(exist_ok=True)
    command = [str(python), '-m', 'dev_mcp', '--policy', str(root / 'policy.toml'),
               '--audit', str(root / 'logs/audit.jsonl'), '--transport', 'http', '--port', str(port)]
    if core_only:
        command.append('--core-only')
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.NORMAL_PRIORITY_CLASS) if os.name == 'nt' else 0
    with (root / 'logs/standalone.log').open('ab') as log:
        child = subprocess.Popen(command, cwd=root, stdout=log, stderr=log, creationflags=flags)
    import psutil
    record = {'pid': child.pid, 'created': psutil.Process(child.pid).create_time(),
              'port': port, 'core_only': core_only}
    (root / 'data/standalone-process.json').write_text(json.dumps(record), encoding='utf-8')
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline and child.poll() is None:
        try:
            return {'running': True, 'pid': child.pid, **probe(port)}
        except (OSError, ValueError, RuntimeError):
            time.sleep(0.3)
    stop(root)
    raise RuntimeError('server did not become ready; inspect logs/standalone.log')


def stop(root: Path) -> dict:
    current = identity(root)
    if current:
        import psutil
        process = current[0]
        # A Windows venv python launcher may have a second interpreter child;
        # killing only the launcher leaves the HTTP listener orphaned.
        owned = process.children(recursive=True) + [process]
        for child in reversed(owned):
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        _, remaining = psutil.wait_procs(owned, timeout=15)
        if remaining:
            raise RuntimeError('owned service processes did not stop')
    return {'running': False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('init', 'start', 'stop', 'status', 'doctor',
                                         'autostart-install', 'uninstall'))
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--workspace', type=Path)
    parser.add_argument('--project', default='Demo')
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--core-only', action='store_true')
    parser.add_argument('--configuration-only', action='store_true',
                        help='doctor: validate installation files before the first start')
    parser.add_argument('--manifest', type=Path, help='explicit existing autostart manifest; opt-in only')
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        if args.action == 'init':
            if args.workspace is None:
                parser.error('init requires --workspace')
            init(root, args.workspace.resolve(), args.project)
            result = {'configured': True}
        elif args.action == 'start':
            result = start(root, args.port, args.core_only)
        elif args.action == 'autostart-install':
            if args.manifest is None:
                parser.error('autostart-install requires --manifest')
            result = autostart(root, args.manifest.resolve(), args.port)
        else:
            current = identity(root)
            result = {'running': current is not None}
            if args.action in ('stop', 'uninstall') and current:
                result = stop(root)
            if args.action == 'uninstall' and args.manifest is not None:
                result.update(autostart(root, args.manifest.resolve(), args.port, remove=True))
            elif args.action == 'doctor':
                result['configuration'] = all((root / p).exists() for p in (
                    'policy.toml', 'machine.toml', 'fleet-projects.toml'))
                result['ticktick_helper'] = (root / 'ticktick-connect.cmd').is_file()
                if current:
                    result.update(probe(current[1]['port']))
                result['ready'] = bool(result['configuration'] and result['ticktick_helper']
                                       and result.get('initialize') and result.get('policy_info'))
                result['remote_connection'] = 'verify authorized tunnel/gateway and ChatGPT separately'
        print(json.dumps(result, ensure_ascii=False))
        if args.action == 'doctor':
            if args.configuration_only:
                return 0 if result['configuration'] and result['ticktick_helper'] else 1
            return 0 if result['ready'] else 1
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
