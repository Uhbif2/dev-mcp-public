import os
import sys
import time
import pytest
from dev_mcp.runner import JobRegistry, is_test_command

@pytest.mark.parametrize('argv', [
    ['py','-3','-m','pytest','test','-q'],
    ['C:/project/.venv/Scripts/pytest.exe','-q'],
    ['python','C:\\project\\pytest-script.py'],
])
def test_pytest_command_has_finite_background_contract(argv):
    assert is_test_command(argv)

def test_service_command_keeps_explicit_background_lifetime():
    assert not is_test_command(['python','server.py'])

def test_background_deadline_really_stops_process_and_records_timeout(tmp_path):
    jobs=JobRegistry(tmp_path/'runs')
    job=jobs.start([sys.executable,'-c','import time;time.sleep(60)'],
                   str(tmp_path),dict(os.environ),timeout_s=0.2)
    try:
        deadline=time.monotonic()+10
        while jobs.get(job.job_id).returncode is None and time.monotonic()<deadline:
            time.sleep(0.05)
        assert job.proc.poll() is not None
        assert job.killed_by_timeout
        assert job.stopped
    finally:
        if job.proc.poll() is None:jobs.stop(job.job_id)
