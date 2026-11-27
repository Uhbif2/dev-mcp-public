"""Finite process execution without pipes or unowned Windows descendants.

Each finite command owns a nested Job Object on Windows. The initial thread is
suspended until assignment, so even a fast launcher cannot escape ownership.
Other platforms use a fresh process group. No host-wide process matching/killing.
"""
from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import threading
import time
from typing import Any

import psutil


class _WindowsJob:
    def __init__(self) -> None:
        import ctypes as ct
        from ctypes import wintypes as wt

        class Basic(ct.Structure):
            _fields_ = [('user', ct.c_int64), ('job_user', ct.c_int64),
                        ('flags', wt.DWORD), ('min_ws', ct.c_size_t),
                        ('max_ws', ct.c_size_t), ('active_limit', wt.DWORD),
                        ('affinity', ct.c_size_t), ('priority', wt.DWORD),
                        ('scheduling', wt.DWORD)]

        class IO(ct.Structure):
            _fields_ = [(name, ct.c_uint64) for name in
                        ('read_ops', 'write_ops', 'other_ops', 'read', 'write', 'other')]

        class Extended(ct.Structure):
            _fields_ = [('basic', Basic), ('io', IO), ('process_memory', ct.c_size_t),
                        ('job_memory', ct.c_size_t), ('peak_process', ct.c_size_t),
                        ('peak_job', ct.c_size_t)]

        self.ct, self.info = ct, Extended()
        self.api = ct.WinDLL('kernel32', use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ct.c_void_p, wt.LPCWSTR]
        self.api.CreateJobObjectW.restype = wt.HANDLE
        self.api.SetInformationJobObject.argtypes = [wt.HANDLE, ct.c_int, ct.c_void_p, wt.DWORD]
        self.api.SetInformationJobObject.restype = wt.BOOL
        self.api.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
        self.api.AssignProcessToJobObject.restype = wt.BOOL
        self.api.TerminateJobObject.argtypes = [wt.HANDLE, wt.UINT]
        self.api.TerminateJobObject.restype = wt.BOOL
        self.api.QueryInformationJobObject.argtypes = [wt.HANDLE, ct.c_int, ct.c_void_p, wt.DWORD, ct.c_void_p]
        self.api.QueryInformationJobObject.restype = wt.BOOL
        self.api.CloseHandle.argtypes = [wt.HANDLE]
        self.api.CloseHandle.restype = wt.BOOL
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ct.WinError(ct.get_last_error())
        self.info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ct.byref(self.info), ct.sizeof(self.info)):
            error = ct.WinError(ct.get_last_error())
            self.close()
            raise error

    def assign(self, proc: subprocess.Popen) -> None:
        if not self.api.AssignProcessToJobObject(self.handle, int(proc._handle)):
            raise self.ct.WinError(self.ct.get_last_error())

    def close(self, *, preserve_children: bool = False) -> None:
        if not self.handle:
            return
        if preserve_children:
            self.info.basic.flags = 0
            if not self.api.SetInformationJobObject(
                    self.handle, 9, self.ct.byref(self.info), self.ct.sizeof(self.info)):
                raise self.ct.WinError(self.ct.get_last_error())
        if not preserve_children:
            if not self.api.TerminateJobObject(self.handle, 1):
                raise self.ct.WinError(self.ct.get_last_error())
            deadline = time.monotonic() + 5
            while True:
                info = self.ct.create_string_buffer(48)
                if not self.api.QueryInformationJobObject(self.handle, 1, info, 48, None):
                    raise self.ct.WinError(self.ct.get_last_error())
                if self.ct.c_uint32.from_buffer(info, 40).value == 0:
                    break
                if time.monotonic() >= deadline:
                    raise OSError('owned process tree did not exit after termination')
                time.sleep(0.01)
        self.api.CloseHandle(self.handle)
        self.handle = None


def start_process(argv: Any, **kwargs: Any) -> subprocess.Popen:
    """Start suspended, attach ownership, then resume; a failed attach fails closed."""
    job = _WindowsJob() if os.name == 'nt' else None
    if job is not None:
        kwargs['creationflags'] = (kwargs.get('creationflags', 0) | 0x4
                                   | subprocess.CREATE_NO_WINDOW)
    else:
        kwargs['start_new_session'] = True
    proc = None
    try:
        proc = subprocess.Popen(argv, **kwargs)
        proc._dev_mcp_job = job
        proc._dev_mcp_group = os.name != 'nt'
        proc._dev_mcp_finish_lock = threading.Lock()
        if job is not None:
            job.assign(proc)
            psutil.Process(proc.pid).resume()
        return proc
    except BaseException:
        if job is not None:
            job.close()
        if proc is not None:
            proc.kill()
            proc.wait(timeout=5)
        raise


def finish_process(proc: subprocess.Popen, *, kill: bool = False,
                   preserve_children: bool = False) -> None:
    """Idempotent, bounded cleanup, shared by timeout, cancellation and callbacks."""
    guard = getattr(proc, '_dev_mcp_finish_lock', threading.Lock())
    with guard:
        job = getattr(proc, '_dev_mcp_job', None)
        if job is not None:
            job.close(preserve_children=preserve_children and not kill)
            proc._dev_mcp_job = None
        elif getattr(proc, '_dev_mcp_group', False):
            if kill or not preserve_children:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            proc._dev_mcp_group = False
        elif kill and proc.poll() is None:
            # Compatibility for pre-existing/unmanaged Popen instances. psutil
            # Process objects guard their kill operations against PID reuse.
            try:
                parent = psutil.Process(proc.pid)
                children = parent.children(recursive=True)
                for child in reversed(children):
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
                parent.kill()
            except psutil.NoSuchProcess:
                pass
        if kill or not preserve_children:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)


def bounded_run(argv: Any, *, timeout: float | None = None,
                capture_output: bool = False, text: bool = False,
                encoding: str | None = None, errors: str | None = None,
                check: bool = False, **kwargs: Any) -> subprocess.CompletedProcess:
    """subprocess.run-compatible finite runner; descendants never hold pipe EOF."""
    if not capture_output:
        raise ValueError('finite runner requires capture_output=True')
    started = time.monotonic()
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        proc = start_process(argv, stdout=stdout, stderr=stderr,
                             stdin=subprocess.DEVNULL, **kwargs)
        expired = False
        try:
            remaining = None if timeout is None else max(0, timeout - (time.monotonic() - started))
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            expired = True
        finally:
            finish_process(proc, kill=expired)
        stdout.seek(0)
        stderr.seek(0)
        out, err = stdout.read(), stderr.read()
    if text or encoding:
        out = out.decode(encoding or 'utf-8', errors or 'replace')
        err = err.decode(encoding or 'utf-8', errors or 'replace')
    if expired:
        raise subprocess.TimeoutExpired(argv, timeout, output=out, stderr=err)
    result = subprocess.CompletedProcess(argv, proc.returncode, out, err)
    if check:
        result.check_returncode()
    return result
