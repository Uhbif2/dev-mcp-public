"""Проба препятствия: заблокированная задача сама сообщает, когда её затор снят.

Зачем (07.10.2026). Датчик телефона (world.py) закрыл один вид затора, а заторов много: нода
недоступна, стенд лежит, порт занят, сервис провайдера отвечает 5xx, файл от владельца ещё не
положен. Все они кончались одинаково — таймер размораживал задачу вслепую, агент упирался снова
(TZ-78: 121 захват), или задача висела, пока препятствие давно ушло. Кто лучше всех знает, как
проверить препятствие? Агент, который только что в него упёрся.

Поэтому fleet_block принимает строку `until: <команда>` — команду, которая завершается с rc=0,
когда препятствие снято (только проверка, без побочных эффектов). Сервер проверяет её по тем же
правилам, что `run`, и прогоняет в фоне с растущей паузой (2 → 4 → 8 → 15 мин). Проба прошла —
задача тает сразу (thaw_frozen); проба есть и падает — таймер её не трогает: фактов не прибавилось.

Состояние — события: `block_probe` (argv при блокировке) и `probe_run` (rc каждого прогона).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import threading
import time
from typing import Any, Callable

from . import bite, process_idle, world

REGISTER_KIND = "block_probe"
RUN_KIND = "probe_run"
TIMEOUT_S = 30.0
BACKOFF_MIN = (2, 4, 8, 15)
MAX_PER_TICK = 4
TICK_EVERY_S = 30.0

# Строка `until:` — с начала строки или после конца фразы в однострочной причине
# (07.10.2026: TZ-373 написал «…нельзя. until: py -3 …» одной строкой — проба не встала).
_UNTIL_RE = re.compile(r"(?im)(?:^|[.;!]\s+)\s*[`*]*until[`*]*\s*:[`*\s]*(.+?)\s*$")

_lock = threading.Lock()
_running = False
_last_tick = 0.0


def from_reason(reason: str) -> str:
    """`until: py -3 tools/check_node.py` → команда; нет строки — ''."""
    match = _UNTIL_RE.search(str(reason or ""))
    if not match:
        return ""
    cmd = match.group(1).strip().strip("`").strip()
    # агенты дописывают пояснение: «until: adb devices — когда телефон появится»
    return re.split(r"\s+[—–]\s+", cmd, maxsplit=1)[0].strip().strip("`")


def register(store: Any, policy: Any, worker_id: str, project: str, tz: str, reason: str) -> str:
    """Записать пробу после fleet_block. → строка для ответа агенту ('' — пробы нет)."""
    raw = from_reason(reason)
    if not raw:
        return ""
    if policy is None:
        return "Проба until не принята: у сервера нет политики исполнения."
    try:
        tokens = [a.strip('"') for a in shlex.split(raw, posix=False)]
        if tokens and tokens[0] == process_idle.COMMAND:
            argv = process_idle.validate(tokens)
        elif tokens and tokens[0] == world.DISK_ADMISSION:
            argv = world.validate_disk_admission(tokens)
        else:
            argv = bite.parse_command(raw, policy)
    except Exception as exc:  # noqa: BLE001 — отказ пробы не отменяет блокировку
        return f"Проба until не принята ({exc}); задача заморожена как обычно."
    with store.tx() as tx:
        tx.record_event(kind=REGISTER_KIND, worker_id=worker_id or None, project=project, tz=tz,
                        payload={"argv": argv})
    return (f"Проба принята: сервер будет прогонять «{' '.join(argv)}» и вернёт задачу в очередь, "
            "как только она завершится с rc=0.")


def _last(tx: Any, project: str, tz: str, kinds: tuple[str, ...]) -> Any:
    return tx.one(
        f"SELECT id,ts,kind,payload FROM events WHERE project=? AND tz=? AND kind IN "
        f"({','.join('?' for _ in kinds)}) ORDER BY id DESC LIMIT 1",
        (project, tz, *kinds),
    )


def current(tx: Any, project: str, tz: str) -> dict[str, Any] | None:
    """Проба текущей блокировки: {argv, block_id, runs: [rc…], last_rc, last_ts} или None."""
    block = _last(tx, project, tz, ("block", "freeze", "thaw", "reactivate", "approve"))
    if block is None or str(block["kind"]) != "block":
        return None
    reg = tx.one(
        "SELECT id,payload FROM events WHERE project=? AND tz=? AND kind=? AND id>? "
        "ORDER BY id DESC LIMIT 1", (project, tz, REGISTER_KIND, int(block["id"])),
    )
    if reg is None:
        return None
    try:
        argv = [str(a) for a in json.loads(str(reg["payload"] or "{}")).get("argv") or []]
    except ValueError:
        return None
    if not argv:
        return None
    runs = [
        (int(json.loads(str(r["payload"] or "{}")).get("rc", -1)), str(r["ts"]))
        for r in tx.query(
            "SELECT ts,payload FROM events WHERE project=? AND tz=? AND kind=? AND id>? ORDER BY id",
            (project, tz, RUN_KIND, int(reg["id"])),
        )
    ]
    return {
        "argv": argv, "block_id": int(block["id"]), "registration_id": int(reg["id"]),
        "runs": [rc for rc, _ in runs],
        "last_rc": runs[-1][0] if runs else None, "last_ts": runs[-1][1] if runs else "",
        "registered_ts": "",
    }


def passed(tx: Any, project: str, tz: str) -> tuple[bool, list[str] | None]:
    """(проба прошла, argv пробы или None — пробы нет)."""
    probe = current(tx, project, tz)
    if probe is None:
        return False, None
    return probe["last_rc"] == 0, probe["argv"]


def _due(probe: dict[str, Any], now: float) -> bool:
    if probe["last_rc"] == 0:
        return False
    if not probe["last_ts"]:
        return True
    from datetime import datetime
    try:
        last = datetime.fromisoformat(probe["last_ts"]).timestamp()
    except ValueError:
        return True
    step = BACKOFF_MIN[min(len(probe["runs"]) - 1, len(BACKOFF_MIN) - 1)]
    return now - last >= step * 60


def _run(argv: list[str], cwd: str) -> int:
    """rc пробы. Вывод не нужен — в DEVNULL: трубу унаследовал бы демон (adb) и вызов висел бы."""
    from ..policy import scrub_env
    from ..runner import child_env
    proc = subprocess.Popen(  # noqa: S603 — argv проверен bite.parse_command по политике run
        argv, cwd=cwd, env=child_env(scrub_env()), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        return int(proc.wait(timeout=TIMEOUT_S))
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=15)
        return -9


def due_probes(store: Any) -> list[tuple[str, str, list[str], str]]:
    now = time.time()
    out: list[tuple[str, int, str, str, list[str], str]] = []
    with store.read() as tx:
        for row in tx.query(
            "SELECT t.project,t.tz,t.priority,p.root FROM tasks t JOIN projects p ON p.name=t.project "
            "WHERE t.state='frozen' ORDER BY t.priority,t.updated_at"
        ):
            probe = current(tx, str(row["project"]), str(row["tz"]))
            if probe and _due(probe, now):
                out.append((probe["last_ts"], int(row["priority"]), str(row["project"]),
                            str(row["tz"]), probe["argv"], str(row["root"])))
    # A timeout can last as long as the first backoff. Priority-first would
    # repeatedly select the same old probes and starve a fresh obstacle forever.
    return [(p, tz, argv, root) for _, _, p, tz, argv, root in
            sorted(out, key=lambda item: item[:4])[:MAX_PER_TICK]]


def run_due(store: Any, policy: Any, runner: Callable[[list[str], str], int] = _run) -> list[str]:
    """Прогнать созревшие пробы (синхронно). → ['P/TZ rc=0', …]."""
    done: list[str] = []
    for project, tz, argv, root in due_probes(store):
        with store.read() as tx:
            binding = current(tx, project, tz)
        if binding is None or binding["argv"] != argv:
            continue
        try:
            if argv[0] == process_idle.COMMAND:
                argv, execute = process_idle.validate(argv), process_idle.run
            elif argv[0] == world.DISK_ADMISSION:
                argv, execute = world.validate_disk_admission(argv), world.disk_admission
            else:
                argv, execute = bite.validate_argv(argv, policy), runner
            rc = execute(argv, root) if root and os.path.isdir(root) else -2
        except Exception:  # noqa: BLE001 — политика ужесточилась или процесс не стартовал
            rc = -1
        with store.tx() as tx:
            latest = current(tx, project, tz)
            task = tx.one("SELECT state FROM tasks WHERE project=? AND tz=?", (project, tz))
            if (latest is None or latest["registration_id"] != binding["registration_id"]
                    or latest["block_id"] != binding["block_id"]
                    or task is None or task["state"] != "frozen"):
                continue
            tx.record_event(kind=RUN_KIND, project=project, tz=tz,
                            payload={"rc": rc, "registration_id": binding["registration_id"],
                                     "block_id": binding["block_id"]})
        done.append(f"{project}/{tz} rc={rc}")
    return done


def tick(store: Any, policy: Any) -> None:
    """Шаг обслуживания: прогон проб в фоне, не чаще TICK_EVERY_S и не в потоке вызова."""
    global _running, _last_tick
    if policy is None or os.environ.get("DEV_MCP_FLEET_SENSORS", "1") == "0":
        return
    with _lock:
        if _running or time.monotonic() - _last_tick < TICK_EVERY_S:
            return
        _running = True
        _last_tick = time.monotonic()

    def work() -> None:
        global _running
        try:
            run_due(store, policy)
        except Exception:  # noqa: BLE001 — датчики не роняют сервер
            pass
        finally:
            with _lock:
                _running = False
    threading.Thread(target=work, name="fleet-block-probe", daemon=True).start()
