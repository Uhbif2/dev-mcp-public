"""An explicitly invoked installer. No installation occurs at import or test time."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

TASK_NAME = "DevMcpPlatformGuard"
TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def package_version(package_dir: Path) -> str:
    """Stable identity of code AND persistent pythonw entrypoint in a candidate."""
    package_dir = package_dir.resolve(strict=True)
    sources = sorted((*package_dir.glob("*.py"), *package_dir.glob("*.pyw")))
    required = {"__init__.py", "__main__.py", "core.py", "install.py",
                "metrics.py", "run_guard.pyw"}
    if not required.issubset({p.name for p in sources}):
        raise RuntimeError("incomplete guard candidate")
    digest = hashlib.sha256()
    for source in sources:
        if source.is_symlink() or not source.is_file():
            raise RuntimeError("guard package contains unsafe source")
        digest.update(source.name.encode("utf-8") + b"\0")
        digest.update(source.read_bytes())
    return digest.hexdigest()[:16]


def task_xml(python: Path, working_directory: Path) -> str:
    """ONLOGON + RestartOnFailure: BootTrigger alone misses current sessions."""
    ET.register_namespace("", TASK_NS)
    def element(name, parent, value=None):
        node = ET.SubElement(parent, "{" + TASK_NS + "}" + name)
        if value is not None:
            node.text = value
        return node
    root = ET.Element("{" + TASK_NS + "}Task", version="1.4")
    triggers = element("Triggers", root)
    logon = element("LogonTrigger", triggers)
    element("Enabled", logon, "true")
    # Installing while logged in must start a watcher without requiring logout.
    # Hourly repetition is also a recovery route if scheduler retries are exhausted.
    started = element("TimeTrigger", triggers)
    element("StartBoundary", started,
            (datetime.now() + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S"))
    repeat = element("Repetition", started)
    element("Interval", repeat, "PT1H")
    element("Enabled", started, "true")
    principals = element("Principals", root)
    principal = element("Principal", principals)
    principal.set("id", "Author")
    element("LogonType", principal, "InteractiveToken")
    element("RunLevel", principal, "LeastPrivilege")
    settings = element("Settings", root)
    element("MultipleInstancesPolicy", settings, "IgnoreNew")
    element("StartWhenAvailable", settings, "true")
    element("Hidden", settings, "true")
    element("ExecutionTimeLimit", settings, "PT0S")
    retry = element("RestartOnFailure", settings)
    element("Interval", retry, "PT1M")
    element("Count", retry, "3")
    actions = element("Actions", root)
    actions.set("Context", "Author")
    execution = element("Exec", actions)
    element("Command", execution, str(python))
    # WorkingDirectory в профиле владельца Планировщик отвергает (0x8007010B, 09.10) —
    # путь к пакету передаётся аргументом, рабочий каталог не задаётся.
    # Scheduler may supply a different LOCALAPPDATA even with InteractiveToken;
    # explicitly bind the installer profile so guard state/rollback uses the
    # SAME state directory. Hex-encode it to avoid Python/shell quote injection.
    local_profile = os.environ.get("LOCALAPPDATA", "")
    profile_code = ("os.environ['LOCALAPPDATA'] = bytes.fromhex('%s').decode('utf-8'); "
                    % local_profile.encode("utf-8").hex()) if local_profile else ""
    # pythonw: sys.stdout/stderr = None — первый print ронял стража с кодом 1 (09.10).
    # Вывод и трассировки — в guard-stdout.log рядом с копией.
    element("Arguments", execution,
            "-c \"import runpy, sys, os; %sd = r'%s'; sys.path.insert(0, d); "
            "sys.stdout = sys.stderr = open(d + r'\\guard-stdout.log', 'a', encoding='utf-8', buffering=1); "
            "sys.argv = ['guard', 'watch']; "
            "runpy.run_module('guard', run_name='__main__')\"" % (profile_code, working_directory))
    # schtasks /XML reads the file as UTF-16; a utf-8 declaration is rejected as malformed.
    return '<?xml version="1.0" encoding="UTF-16"?>\n' + ET.tostring(root, encoding="unicode")


def schtasks_args(xml_path: Path) -> list[str]:
    return ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"]


def _pid_alive(pid: int) -> bool:
    """Inspect the heartbeat PID itself, never a command-line substring."""
    if pid <= 0 or pid == os.getpid():
        return False
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
        kernel.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = kernel.OpenProcess(0x00100000, 0, pid)  # SYNCHRONIZE, read-only
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def _pulse(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _prove_running_guard(
    path: Path, version: str, *, before: tuple[object, object],
    alive: Callable[[int], bool], clock: Callable[[], float],
    sleep: Callable[[float], None], duration_s: float, poll_s: float,
) -> bool:
    """Require one persistent living PID and >=2 NEW beats over the full window."""
    if duration_s <= 0 or poll_s <= 0 or poll_s > duration_s:
        raise ValueError("invalid guard heartbeat verification window")
    start = last_beat = clock()
    last_stamp = before
    candidate_pid: int | None = None
    beats = 0
    while True:
        now = clock()
        pulse = _pulse(path)
        if pulse.get("version") == version:
            pid = pulse.get("pid")
            if type(pid) is not int or pid <= 0:
                return False
            if pid == before[0]:
                return False  # /Run may silently IgnoreNew and leave the old task alive
            if candidate_pid is not None and pid != candidate_pid:
                return False  # restart mid-verification is not continuous life
            if not alive(pid):
                return False
            stamp = (pid, pulse.get("time"))
            if pulse.get("time") and stamp != last_stamp:
                candidate_pid = pid
                last_stamp = stamp
                last_beat = now
                beats += 1
        elif candidate_pid is not None:
            return False  # wrong-version guard replaced our observed process
        if now - last_beat > max(2 * poll_s, duration_s / 2):
            return False
        if now - start >= duration_s:
            return candidate_pid is not None and beats >= 2
        sleep(min(poll_s, max(0, duration_s - (now - start))))


def _task_backup(runner) -> str | None:
    """Query the old scheduled task BEFORE /Create /F; uncertain state fails closed."""
    result = runner(["schtasks", "/Query", "/TN", TASK_NAME, "/XML"],
                    check=False, capture_output=True, text=True)
    if result.returncode:
        error = str((result.stderr or "") + (result.stdout or "")).casefold()
        if any(part in error for part in (
            "cannot find", "does not exist", "not found",
            "не удается найти", "не удаётся найти", "не найден",
        )):
            return None
        raise RuntimeError("cannot safely determine previous scheduled task")
    xml = result.stdout
    if isinstance(xml, bytes):
        xml = xml.decode("utf-16" if xml.startswith((b"\xff\xfe", b"\xfe\xff"))
                         else "utf-8-sig", errors="replace")
    if not isinstance(xml, str):
        raise RuntimeError("scheduled task backup is not XML")
    try:
        parsed = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise RuntimeError("scheduled task backup is invalid XML") from exc
    if parsed.tag != "{" + TASK_NS + "}Task":
        raise RuntimeError("scheduled task backup has unexpected root")
    return xml


def _restore_task(runner, previous_xml: str | None) -> None:
    if previous_xml is None:
        command = ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]
        result = runner(command, check=False, capture_output=True, text=True)
    else:
        with tempfile.TemporaryDirectory(prefix="guard-schtasks-rollback-") as scratch:
            original = Path(scratch) / "previous.xml"
            original.write_text(previous_xml, encoding="utf-16")
            result = runner(schtasks_args(original), check=False,
                            capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("guard heartbeat missing AND scheduled task rollback failed")


def publish_launcher(runtime: Path, version: str) -> None:
    """Publish a previously verified supervisor only after successful canary.

    Staging must NEVER modify the persistent autostart entrypoint: a crash
    before candidate validation must reboot into the old trusted supervisor.
    """
    target = runtime / version
    entry = target / "run_guard.pyw"
    if (not version or not version.isalnum() or target.is_symlink()
            or entry.is_symlink() or not entry.is_file()):
        raise RuntimeError("untrusted staged guard launcher")
    # A version-named folder is not proof of its bytes. Verify the entire
    # staged package before allowing its supervisor to become persistent.
    library = target / "guard"
    sources = sorted([*library.glob("*.py"), entry], key=lambda p: p.name)
    required = {"__init__.py", "__main__.py", "core.py", "install.py",
                "metrics.py", "run_guard.pyw"}
    if (library.is_symlink() or not required.issubset({p.name for p in sources})
            or any(p.is_symlink() or not p.is_file() for p in sources)):
        raise RuntimeError("untrusted staged guard package")
    digest = hashlib.sha256()
    for source in sources:
        digest.update(source.name.encode("utf-8") + b"\0")
        digest.update(source.read_bytes())
    if digest.hexdigest()[:16] != version:
        raise RuntimeError("staged guard package checksum mismatch")
    launcher = runtime / "run_guard.pyw"
    if launcher.is_symlink():
        raise RuntimeError("untrusted stable guard launcher")
    candidate = entry.read_bytes()
    if launcher.is_file() and launcher.read_bytes() == candidate:
        return
    runtime.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=runtime, prefix="run_guard.",
                                     suffix=".new", delete=False) as staged:
        staged_path = Path(staged.name)
        staged.write(candidate)
        staged.flush()
        os.fsync(staged.fileno())
    try:
        os.replace(staged_path, launcher)
    finally:
        staged_path.unlink(missing_ok=True)


def install(package_dir: Path | None = None, *, base: Path | None = None,
            runner=subprocess.run, register: bool = False,
            heartbeat_path: Path | None = None,
            process_alive: Callable[[int], bool] = _pid_alive,
            clock: Callable[[], float] = time.monotonic,
            sleep: Callable[[float], None] = time.sleep,
            verification_s: float = 30.0, poll_s: float = 1.0) -> Path:
    """Copy current guard to immutable SHA folder, register only on explicit call.

    Do not invoke during fleet work. The guard source is protected from self-deploy.
    """
    package_dir = (package_dir or Path(__file__).resolve().parent).resolve()
    # Копия — в data/guard-runtime живого каталога (git игнорирует, флоту закрыто tree_deny).
    # %LOCALAPPDATA% не годится: запись туда из сессии Claude Code изолирована, и Планировщик
    # эту папку не видел (FileNotFoundError, 09.10).
    base = (base or package_dir.parent / "data" / "guard-runtime").resolve()
    source_files = sorted(package_dir.glob("*.py"))
    target = base / package_version(package_dir)
    dest = target / "guard"
    if not dest.exists():
        dest.mkdir(parents=True, exist_ok=False)
        for source in source_files:
            shutil.copy2(source, dest / source.name)
    # A pre-existing version is not trusted solely by its directory name.
    # Never launch an overwritten/tampered immutable copy.
    if any(not (dest / source.name).is_file()
           or (dest / source.name).read_bytes() != source.read_bytes()
           for source in source_files):
        raise RuntimeError("installed guard version has invalid contents")
    python = Path(sys.executable).resolve()
    # pythonw has no console window: periodic jobs must not flash a terminal.
    hidden_python = python.with_name("pythonw.exe")
    if hidden_python.is_file():
        python = hidden_python
    xml = target / "guard-task.xml"
    xml.write_text(task_xml(python, target), encoding="utf-16")
    # The supervisor must be versioned too: candidate handoff runs its own
    # verified entrypoint, while autostart keeps using the last proven one.
    entry = package_dir / "run_guard.pyw"
    staged_entry = target / "run_guard.pyw"
    if not staged_entry.exists():
        shutil.copy2(entry, staged_entry)
    if staged_entry.is_symlink() or staged_entry.read_bytes() != entry.read_bytes():
        raise RuntimeError("installed guard launcher has invalid contents")
    if not register:
        return target
    # A successful schtasks /Create only proves that Scheduler accepted XML,
    # NOT that the executable started. Query/backup first because /F replaces
    # the previous definition. Never touch the stable autostart launcher until
    # a new process and changing version-bound heartbeat have been observed.
    previous_xml = _task_backup(runner)
    pulse_path = heartbeat_path or base.parent / "guard-heartbeat.json"
    old_pulse = _pulse(pulse_path)
    before = (old_pulse.get("pid"), old_pulse.get("time"))
    installed_task = False
    # The installed guard ships as a self-contained stdlib-only package.
    # Never import dev_mcp here: recovery must work even if the server is
    # missing or cannot import. The fleet's preflight reuses this primitive
    # when the guard package is available.
    from .verified import install_verified

    candidate_pid: list[int | None] = [None]
    failure: list[Exception | None] = [None]

    def register_task() -> None:
        nonlocal installed_task
        try:
            result = runner(schtasks_args(xml), check=False, capture_output=True, text=True)
            installed_task = True  # an error could follow partial registration
            if result.returncode:
                raise RuntimeError("schtasks registration failed")
            started = runner(["schtasks", "/Run", "/TN", TASK_NAME],
                             check=False, capture_output=True, text=True)
            if started.returncode:
                raise RuntimeError("schtasks could not start registered guard")
        except Exception as exc:
            failure[0] = exc
            raise

    def current_pulse() -> tuple[int, object] | None:
        info = _pulse(pulse_path)
        if info.get("version") != target.name:
            if candidate_pid[0] is not None:
                raise RuntimeError("guard heartbeat changed version")
            return None
        pid, stamp = info.get("pid"), info.get("time")
        if type(pid) is not int or pid <= 0 or pid == before[0]:
            if candidate_pid[0] is not None:
                raise RuntimeError("guard heartbeat reused previous pid")
            return None
        if candidate_pid[0] is not None and pid != candidate_pid[0]:
            raise RuntimeError("guard process restarted during verification")
        if not process_alive(pid):
            raise RuntimeError("guard heartbeat pid is dead")
        candidate_pid[0] = pid
        return (pid, stamp) if stamp else None

    def live() -> bool:
        return candidate_pid[0] is None or process_alive(candidate_pid[0])

    def restore() -> None:
        if installed_task:
            _restore_task(runner, previous_xml)

    if not install_verified(
        register_task, alive=live, pulse=current_pulse, rollback=restore,
        duration_s=verification_s, poll_s=poll_s, clock=clock, sleep=sleep,
    ):
        if failure[0] is not None:
            raise failure[0]
        raise RuntimeError("guard installed but did not prove live changing heartbeat")
    publish_launcher(base, target.name)
    return target
