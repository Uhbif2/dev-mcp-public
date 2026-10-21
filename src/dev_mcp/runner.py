"""Фоновые процессы для `run`.

Синхронный `run` живёт прямо в server.py (`subprocess.run` + захват вывода). Здесь —
только долгоживущее: TUN-сеанс, watcher, dev-server. Клиент получает `job_id`,
дальше опрашивает `run_status` и гасит `run_stop`.

Процесс стартует в НОВОЙ группе процессов (Windows: CREATE_NEW_PROCESS_GROUP), чтобы
`run_stop` мог убить всё дерево через `taskkill /T`, а не только корень. Вывод пишется
в файл `<runs_dir>/<job_id>.log` — назад клиенту отдаётся хвост, не весь файл.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
_CREATE_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

# Символы, которые при неверной кодировке всплывают вместо кириллицы: псевдографика cp866,
# Latin-1-мусор cp1251 из диапазона 0x80–0xBF, управляющие.
_MOJIBAKE = set(range(0x2500, 0x2580)) | set(range(0x80, 0xC0)) | {0x0402, 0x0403, 0x201A, 0x0453, 0x201E}


def _cyr_score(text: str) -> int:
    good = sum(1 for ch in text if 'а' <= ch.lower() <= 'я' or ch in 'ёЁ')
    bad = sum(1 for ch in text if ord(ch) in _MOJIBAKE)
    return good - 3 * bad


def decode_output(raw: bytes) -> str:
    """Вывод дочернего процесса → текст, построчно.

    В журнале смешаны заголовок сервера (utf-8) и сырые байты процесса: python без
    PYTHONUTF8 пишет в файл cp1251, консольные утилиты Windows — cp866. Строка, которая
    не читается как utf-8, декодируется той из cp1251/cp866, что даёт больше кириллицы и
    меньше мусора. Раньше всё читалось как utf-8 с заменой — агент видел «�����».
    """
    out: list[str] = []
    for line in raw.split(b'\n'):
        try:
            out.append(line.decode('utf-8'))
            continue
        except UnicodeDecodeError:
            pass
        cands = [line.decode(enc, errors='replace') for enc in ('cp1251', 'cp866')]
        out.append(max(cands, key=_cyr_score))
    return '\n'.join(out)


def child_env(env: dict[str, str]) -> dict[str, str]:
    """Python-потомки пишут вывод в UTF-8, даже когда он перенаправлен в файл."""
    out = dict(env)
    out.setdefault('PYTHONUTF8', '1')
    out.setdefault('PYTHONIOENCODING', 'utf-8')
    return out


def is_test_command(argv: list[str]) -> bool:
    """A pytest run has an end; background mode must not turn it into a daemon."""
    return any(re.search(r'(?i)(?:^|[/\\\s])pytest(?:\.exe|-script\.py)?(?:$|\s)', str(arg))
               for arg in argv)


@dataclass
class Job:
    job_id: str
    argv: list[str]
    cwd: str
    log_path: Path
    started_at: float
    proc: subprocess.Popen
    _log_fh: object = None
    finished_at: float | None = None
    returncode: int | None = None
    stopped: bool = False
    # Задача не запускалась фоновой: её начал синхронный `run`, но она не уложилась
    # в бюджет одного вызова MCP и была передана сюда, чтобы клиент не потерял
    # результат вместе с оборванным соединением. См. JobRegistry.adopt.
    promoted: bool = False
    killed_by_timeout: bool = False


@dataclass
class JobRegistry:
    runs_dir: Path
    _jobs: dict[str, Job] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    max_jobs: int = 32

    def __post_init__(self) -> None:
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def start(self, argv: list[str], cwd: str, env: dict[str, str], *,
              timeout_s: float | None = None) -> Job:
        with self._lock:
            self._reap_locked()
            alive = [j for j in self._jobs.values() if j.returncode is None]
            if len(alive) >= self.max_jobs:
                raise RuntimeError(f'слишком много фоновых задач ({len(alive)}) — останови лишние')
            job_id = uuid.uuid4().hex[:12]
            log_path = self.runs_dir / f'{job_id}.log'
            fh = log_path.open('w', encoding='utf-8', errors='replace')
            fh.write(f'# {" ".join(argv)}\n# cwd={cwd}\n\n')
            fh.flush()
            proc = subprocess.Popen(  # noqa: S603 — argv собран политикой, shell=False
                argv,
                cwd=cwd,
                env=env,
                stdout=fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                shell=False,
                creationflags=_CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW,
            )
            job = Job(
                job_id=job_id,
                argv=list(argv),
                cwd=cwd,
                log_path=log_path,
                started_at=time.time(),
                proc=proc,
                _log_fh=fh,
            )
            self._jobs[job_id] = job
        if timeout_s is not None:
            threading.Thread(target=self._kill_after, args=(job_id, max(0.0, timeout_s)),
                             daemon=True).start()
        return job

    def adopt(
        self,
        proc: subprocess.Popen,
        argv: list[str],
        cwd: str,
        log_path: Path,
        log_fh: object,
        *,
        started_at: float,
        remaining_s: float | None = None,
        on_exit: Callable[[Job], None] | None = None,
    ) -> Job:
        """Принять УЖЕ ЗАПУЩЕННЫЙ процесс синхронного `run` как фоновую задачу.

        Зачем. Синхронный вызов наружу идёт через шлюз, а у шлюза свой потолок на
        длительность одного вызова. Раньше при его превышении соединение рвалось,
        и агент видел «dev-mcp отвалился», хотя команда на машине продолжала (или
        уже закончила) работу — результат просто некому было забрать, и агент
        запускал сборку заново. Теперь вызов возвращается вовремя и отдаёт job_id:
        процесс НЕ убивается, лог тот же самый, дальше обычные run_status/run_stop.

        Лимит max_jobs здесь не проверяем: процесс уже живёт, отказ ничего бы не
        отменил — он только потерял бы ссылку на него.
        """
        with self._lock:
            self._reap_locked()
            job_id = uuid.uuid4().hex[:12]
            job = Job(
                job_id=job_id,
                argv=list(argv),
                cwd=cwd,
                log_path=log_path,
                started_at=started_at,
                proc=proc,
                _log_fh=log_fh,
                promoted=True,
            )
            self._jobs[job_id] = job
        if remaining_s is not None and remaining_s > 0:
            # Заявленный клиентом timeout_s остаётся в силе: уход в фон продлевает
            # ЖИЗНЬ ОТВЕТА, а не жизнь процесса. Сторожевой поток добивает задачу в
            # исходный срок — иначе «вечная» команда (watcher, сервер) висела бы до
            # перезапуска dev-mcp, не считаясь ни в одном лимите.
            threading.Thread(
                target=self._kill_after, args=(job_id, remaining_s), daemon=True,
            ).start()
        if on_exit is not None:
            # Журнал флота обязан получить фактический rc и для ушедшей в фон
            # команды: приёмка (fleet_review accept) считает доказательством
            # СОБСТВЕННОЕ run-событие ревьюера с rc=0, а прогон тестов как раз и
            # есть та команда, которая в бюджет вызова не укладывается. Без этого
            # потока длинная acceptance-команда навсегда оставалась бы без rc.
            threading.Thread(
                target=self._notify_on_exit, args=(job_id, on_exit), daemon=True,
            ).start()
        return job

    def _kill_after(self, job_id: str, delay_s: float) -> None:
        time.sleep(delay_s)
        job = self._jobs.get(job_id)
        if job is None or job.returncode is not None:
            return
        job.killed_by_timeout = True
        self.stop(job_id)

    def when_done(self, job_id: str, on_exit: Callable[[Job], None]) -> None:
        """Вызвать on_exit, когда фоновая задача закончится (поток-наблюдатель)."""
        threading.Thread(
            target=self._notify_on_exit, args=(job_id, on_exit), daemon=True,
        ).start()

    def _notify_on_exit(self, job_id: str, on_exit: Callable[[Job], None]) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        try:
            job.proc.wait()
        except OSError:
            return
        self.get(job_id)  # реап: проставит returncode/finished_at и закроет лог
        try:
            on_exit(job)
        except Exception:  # noqa: BLE001 — телеметрия не ломает исполнение
            return

    def _reap_locked(self) -> None:
        for job in self._jobs.values():
            if job.returncode is None:
                rc = job.proc.poll()
                if rc is not None:
                    job.returncode = rc
                    job.finished_at = time.time()
                    if job._log_fh is not None:
                        try:
                            job._log_fh.close()
                        except OSError:
                            pass
                        job._log_fh = None

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            self._reap_locked()
            return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        with self._lock:
            self._reap_locked()
            return sorted(self._jobs.values(), key=lambda j: j.started_at, reverse=True)

    def stop(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None:
            return False
        if job.returncode is not None:
            return True
        try:
            stopped = subprocess.run(  # noqa: S603,S607 — фиксированный argv, наш процесс
                ['taskkill', '/F', '/T', '/PID', str(job.proc.pid)],
                capture_output=True,
                timeout=15,
                creationflags=_CREATE_NO_WINDOW,
            )
            if stopped.returncode != 0 and job.proc.poll() is None:
                raise OSError('taskkill did not stop the job')
        except (OSError, subprocess.TimeoutExpired):
            try:
                job.proc.send_signal(getattr(signal, 'CTRL_BREAK_EVENT', signal.SIGTERM))
            except (OSError, ValueError):
                pass
            job.proc.terminate()
        try:
            job.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            job.proc.kill()
        job.stopped = True
        with self._lock:
            self._reap_locked()
        return True

    @staticmethod
    def tail(job: Job, max_chars: int = 4000) -> str:
        try:
            text = decode_output(job.log_path.read_bytes())
        except OSError:
            return '(лог недоступен)'
        return text if len(text) <= max_chars else '…' + text[-max_chars:]


def describe_job(job: Job) -> str:
    if job.returncode is None:
        state = f'выполняется (pid {job.proc.pid}, {int(time.time() - job.started_at)} с)'
    elif job.killed_by_timeout:
        state = f'убита по таймауту, код возврата {job.returncode}'
    else:
        how = 'остановлена' if job.stopped else 'завершилась'
        state = f'{how}, код возврата {job.returncode}'
    origin = ' (ушла в фон из синхронного run)' if job.promoted else ''
    return f'{job.job_id}: {state}{origin}\nкоманда: {" ".join(job.argv)}\ncwd: {job.cwd}'


def clean_env_for_child(env: dict[str, str]) -> dict[str, str]:
    """Гарантия непустого PATH/окружения (scrub_env мог срезать слишком много)."""
    out = dict(env)
    if 'PATH' not in {k.upper() for k in out}:
        out['PATH'] = os.environ.get('PATH', '')
    return out
