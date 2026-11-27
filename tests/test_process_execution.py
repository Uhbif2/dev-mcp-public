"""Real descendants and inherited output; no production process is touched."""
import json
import os
from pathlib import Path
import sys
import time

import psutil

from dev_mcp.fleet.exec_preflight import run_command
from dev_mcp.process_execution import bounded_run
from dev_mcp.runner import JobRegistry


def parent(tmp_path: Path, *, parent_sleep=30) -> tuple[list[str], Path]:
    marker=tmp_path/'child.json'
    child="import time; time.sleep(30)"
    script=tmp_path/'parent.py'
    script.write_text("import subprocess,sys,time,json\nfrom pathlib import Path\n"
                      f"p=subprocess.Popen([sys.executable,'-c',{child!r}])\n"
                      f"Path({str(marker)!r}).write_text(json.dumps({{'pid':p.pid}}))\n"
                      "print('partial output',flush=True)\n"
                      f"time.sleep({parent_sleep})\n",encoding='utf-8')
    return [sys.executable,str(script)],marker


def assert_gone(marker: Path):
    child=json.loads(marker.read_text(encoding="utf-8"))['pid']
    try:
        process=psutil.Process(child)
        process.wait(timeout=2)
    except psutil.NoSuchProcess:
        pass


def test_timeout_bounds_inherited_pipe_and_reaps_grandchild(tmp_path):
    argv,marker=parent(tmp_path)
    started=time.monotonic()
    # 3 s: a cold interpreter (uv-managed Python, CI) may need >0.7 s before its first print.
    result=run_command(argv,cwd=tmp_path,timeout_s=3)
    assert result.outcome=='timed_out'
    assert time.monotonic()-started<6
    assert 'partial output' in result.stdout
    assert_gone(marker)


def test_finite_parent_exit_cannot_leave_a_resource_owning_descendant(tmp_path):
    argv,marker=parent(tmp_path,parent_sleep=0)
    result=bounded_run(argv,cwd=tmp_path,capture_output=True,text=True,timeout=3)
    assert result.returncode==0
    assert_gone(marker)


def test_background_cancellation_and_completion_callback_share_tree_cleanup(tmp_path):
    argv,marker=parent(tmp_path)
    registry=JobRegistry(tmp_path/'runs')
    job=registry.start(argv,str(tmp_path),dict(os.environ),timeout_s=10)
    completed=[]
    registry.when_done(job.job_id,lambda done: completed.append(done.returncode))
    try:
        end=time.monotonic()+3
        while not marker.exists() and time.monotonic()<end:
            time.sleep(0.02)
        assert marker.exists()
        registry.stop(job.job_id)
        end=time.monotonic()+3
        while not completed and time.monotonic()<end:
            time.sleep(0.02)
        assert completed
        assert_gone(marker)
    finally:
        registry.stop(job.job_id)


def test_resource_wait_consumes_execution_budget_and_busy_never_launches(tmp_path,monkeypatch):
    from dev_mcp.fleet import world
    calls=[]
    def acquire(*args,**kw):
        calls.append(kw['wait_s'])
        return None,'owned by another live command'
    monkeypatch.setattr(world,'hold_blocking',acquire)
    result=run_command([sys.executable,'-c',"raise RuntimeError('must not launch')"],
                       cwd=tmp_path,timeout_s=0.1)
    assert result.outcome=='not_started_busy'
    assert 0<=calls[0]<=0.1
