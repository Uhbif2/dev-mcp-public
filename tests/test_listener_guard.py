import copy
import threading
import json
import subprocess
import sys

import pytest

from dev_mcp.listener_guard import recovery_pid, watch


def identity():
    return {'pid': 10, 'instances': {
        'dev': {'pid': 20, 'port': 8766, 'state': 'running'},
        'core': {'pid': 30, 'port': 8765, 'state': 'running'}}}


def test_only_own_verified_dev_ancestor_can_be_recovered():
    ancestors = [{'pid': 5, 'command': 'backend'},
                 {'pid': 20, 'command': 'python -m mcp_proxy --port 8766 --host 127.0.0.1'},
                 {'pid': 10, 'command': 'pythonw mcp_launcher.py'}]
    assert recovery_pid(identity(), ancestors) == 20
    assert recovery_pid(identity(), ancestors[:-1]) is None  # foreign supervisor
    assert recovery_pid(identity(), [ancestors[0], ancestors[-1]]) is None  # foreign state PID
    assert recovery_pid(identity(), [{**a, 'command': a['command'].replace('8766', '8765')}
                                     for a in ancestors]) is None
    stale = copy.deepcopy(identity())
    stale['instances']['core']['pid'] = 20
    assert recovery_pid(stale, ancestors) is None
    stale['instances']['core']['pid'] = 30
    stale['instances']['dev']['state'] = 'restarting'
    assert recovery_pid(stale, ancestors) is None


def test_no_startup_or_transient_restarts_and_lost_listener_recovers_once():
    stop = threading.Event()
    observations = iter([False, False, False, True, False, False, True, False, False, False])
    seen = []
    repairs = []
    def probe():
        value = next(observations)
        seen.append(value)
        return value
    def repair(_audit):
        repairs.append(len(seen))
        return True
    watch(stop, None, probe=probe, repair=repair, interval_s=0)
    assert repairs == [10]


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows process handle API')
@pytest.mark.parametrize('already_exited', [False, True])
def test_bound_handle_effect_is_limited_to_owned_test_child(tmp_path, monkeypatch, already_exited):
    from dev_mcp import listener_guard as guard
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        state = identity()
        state['instances']['dev']['pid'] = child.pid
        path = tmp_path/'state.json'
        path.write_text(json.dumps(state), encoding='utf-8')
        monkeypatch.setattr(guard, 'STATE', path)
        monkeypatch.setattr(guard, 'port_up', lambda: False)
        monkeypatch.setattr(guard, '_ancestors', lambda: [
            {'pid': 10}, {'pid': child.pid, 'command': 'python -m mcp_proxy --port 8766'}])
        records = []
        class Audit:
            def record(self, *args, **kwargs):
                records.append(args)
        if already_exited:
            child.terminate()
            child.wait(timeout=5)
        assert guard.recover(Audit()) is (not already_exited)
        child.wait(timeout=5)
        assert len(records) == int(not already_exited)
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)
