"""Ресурсы реального мира: телефон, нода, стенд, ВМ, покупка.

Зачем (07.10.2026). TZ-78 Nova VPN — живая сессия на телефоне — брали 121 раз, 77 раз
блокировали (61 раз «нет устройства») и 72 раза размораживали таймером с паузой до 3 ч: телефон,
подключённый владельцем в паузу, система не видела. А за TZ-78 стояли 36 задач — вплоть до
юнит-экономики, которой сотовый канал Android не нужен. Отсюда два правила:

1. **Датчик.** Ресурс с `sensor` в fleet-projects.toml опрашивается в фоне. Ресурса нет — задачи
   с ним не выдаются и не размораживаются таймером; появился — замороженные на нём задачи сразу
   идут в ready. Состояние — события `resource` на переходах (`path_norm = resource:<имя>`), без
   своей таблицы: его читают и сервер, и пульт.
2. **Проверка мира не наследуется.** Задача, держащая ресурс мира, которого нет у потребителя, —
   задача-доказательство: потребителю нужны её входы (код), а не её живой прогон. Такая
   зависимость считается выполненной, когда выполнены входы задачи-доказательства (рекурсивно,
   с точки зрения того же потребителя). Ошибся — агент блокируется с её именем в причине,
   и thaw ждёт её приёмки: цена промаха — один захват, цена старого правила — стоящий проект.

Конфиг (fleet-projects.toml):

    [resources."телефон"]
    world = true
    sensor = "adb"        # спрашивает уже запущенный демон adb на 127.0.0.1:5037
    need = "подключи Android-телефон"

Ресурс одного устройства (20.11.2026): `device = "<regex>"` — ресурс up, только когда готово
(`device`) устройство, чья строка `adb devices -l` (serial, product:, model:, device:) подходит
под regex без учёта регистра; другие подключённые телефоны его не поднимают. Пример:

    [resources."планшет-samsung"]
    world = true
    sensor = "adb"
    device = "model:SM_T735"
    need = "подключи планшет Tablet TAB-A1 (ADB по USB или Wi-Fi)"

Без `device` ресурс, как раньше, up при любом готовом устройстве. Ошибка в regex — unknown.

Демон adb — один, свой и долгоживущий (05.11.2026). Раньше датчик только спрашивал демон, а демон
поднимал первый `run adb …` агента — внутри Job Object команды с KILL_ON_JOB_CLOSE: команда
кончилась — демон убит, телефон видит обрыв, следующий вызов поднимает демон заново и телефон
снова спрашивает «Разрешить отладку?». Без демона датчик видел `unknown`, и подключённый владельцем
телефон система не замечала. Теперь: `adb = "<путь>"` у ресурса — датчик сам поднимает демон этим
бинарём вне задания (`ensure_adb_server`), а `run adb …` агентов идёт тем же бинарём (`pin_adb`):
клиент другой версии демон не перезапускает.
"""
from __future__ import annotations
from datetime import datetime, timezone

import json
import os
import re
import socket
import threading
import time
from typing import Any, Callable, Iterable, Mapping

from . import goals

UP, DOWN, UNKNOWN = "up", "down", "unknown"
SENSE_EVERY_S = 30.0
EVENT_KIND = "resource"

_lock = threading.Lock()
_last_started = 0.0
_pending: dict[str, tuple[str, str]] = {}   # результат фонового опроса, ещё не записанный в базу
_running = False


def config() -> dict[str, dict[str, Any]]:
    return {str(k): v for k, v in goals.resources_info().items() if isinstance(v, dict)}


def world_names(cfg: Mapping[str, Mapping[str, Any]] | None = None) -> frozenset[str]:
    cfg = config() if cfg is None else cfg
    return frozenset(name for name, info in cfg.items() if info.get("world") or info.get("sensor"))


def path_key(name: str) -> str:
    return f"resource:{name}"


# ---- датчики ---------------------------------------------------------------------------

def _adb_devices(host: str = "127.0.0.1", port: int = 5037, timeout: float = 2.0) -> tuple[str, str]:
    """Протокол adb-сервера: `host:devices` → список `serial\\tstate`. Клиента не запускает."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            cmd = b"host:devices"
            sock.sendall(b"%04x" % len(cmd) + cmd)
            status = _recv(sock, 4)
            if status != b"OKAY":
                return UNKNOWN, f"adb-сервер ответил {status!r}"
            size = int(_recv(sock, 4), 16)
            body = _recv(sock, size).decode("utf-8", "replace") if size else ""
    except (OSError, ValueError) as exc:
        return UNKNOWN, f"adb-сервер недоступен: {type(exc).__name__}"
    ready = [line.split("\t")[0] for line in body.splitlines() if line.endswith("\tdevice")]
    if ready:
        return UP, "устройства: " + ", ".join(ready)
    other = [line.replace("\t", " ") for line in body.splitlines() if line.strip()]
    return DOWN, ("нет готовых устройств" + (f" ({'; '.join(other)})" if other else "")
                  + "; " + _usb_android_hint())


_USB_HINT_CACHE: tuple[float, str] = (0.0, "")


def usb_phone_hint(names: list[str]) -> str:
    """Только то, что Windows видит сейчас (-PresentOnly). 05.11.2026: прежний текст выдавал
    догадку за факт — «HiOS усыпил отладку, разблокируй экран», хотя экран был разблокирован.
    На деле PHONEB был на USB с PID 201C (передача файлов), без интерфейса ADB: отладка по USB
    на телефоне выключена. С интерфейсом ADB и пустым adb — ждёт подтверждения ключа."""
    if not names:
        return "Windows не видит телефон по USB вовсе (кабель «только зарядка», порт или режим на телефоне)"
    shown = ", ".join(names[:3])
    if any("adb" in n.casefold() for n in names):
        return ("телефон на USB с интерфейсом ADB (" + shown + "), но adb его не видит: на телефоне "
                "ждёт «Разрешить отладку по USB?» или отладку надо выключить и включить")
    return ("телефон на USB без интерфейса ADB (" + shown + "): на телефоне выключена «Отладка по USB» "
            "(Windows видит только передачу файлов) — включи её в «Для разработчиков»")


def _usb_android_hint() -> str:
    """Что про телефон видит сама Windows: USB-устройство Android есть, а adb пуст — ждёт отладка
    по USB / разрешение на телефоне; USB-устройства нет вовсе — кабель «только зарядка», порт,
    режим на телефоне. Кэш 60 с: PowerShell дорог, датчик зовут часто."""
    import subprocess
    import time
    global _USB_HINT_CACHE
    at, text = _USB_HINT_CACHE
    if text and time.monotonic() - at < 60:
        return text
    script = (
        "Get-PnpDevice -PresentOnly -EA 0 | Where-Object { $_.InstanceId -like 'USB*' -and "
        "($_.FriendlyName -match 'ADB|Android|MTP|Composite|PHONEB|Portable|MediaTek|MTK') } | "
        "ForEach-Object { $_.FriendlyName }"
    )
    try:
        done = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        names = sorted({x.strip() for x in str(done.stdout or "").splitlines() if x.strip()})
    except (OSError, subprocess.SubprocessError) as exc:
        return f"USB не проверен ({type(exc).__name__})"
    text = usb_phone_hint(names)
    _USB_HINT_CACHE = (time.monotonic(), text)
    return text


def _recv(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("adb-сервер закрыл соединение")
        buf += chunk
    return buf


def _tcp(info: Mapping[str, Any]) -> tuple[str, str]:
    """`sensor = "tcp"`, host, port: соединение открылось — up (нода, стенд, порт сервиса)."""
    host, port = str(info.get("host") or ""), int(info.get("port") or 0)
    if not host or not port:
        return UNKNOWN, "tcp: в конфиге нет host/port"
    try:
        with socket.create_connection((host, port), timeout=float(info.get("timeout", 4))):
            return UP, f"{host}:{port} принимает соединения"
    except OSError as exc:
        return DOWN, f"{host}:{port}: {type(exc).__name__}"


def _http(info: Mapping[str, Any]) -> tuple[str, str]:
    """`sensor = "http"`, url: ответ 2xx/3xx — up. Мимо системного прокси (он вешает отдельные домены)."""
    import urllib.error
    import urllib.request
    url = str(info.get("url") or "")
    if not url:
        return UNKNOWN, "http: в конфиге нет url"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=float(info.get("timeout", 6))) as resp:
            code = int(resp.status)
    except urllib.error.HTTPError as exc:
        code = int(exc.code)
    except (OSError, ValueError) as exc:
        return DOWN, f"{url}: {type(exc).__name__}"
    return (UP if code < 400 else DOWN), f"{url}: HTTP {code}"


def _disk(info: Mapping[str, Any]) -> tuple[str, str]:
    import shutil
    try:
        free = shutil.disk_usage(str(info["path"])).free
        minimum = int(info.get("min_free_bytes", 8 * 1024 ** 3))
        return (UP if free >= minimum else DOWN,
                f"свободно {free / 1024**3:.1f} ГиБ; требуется {minimum / 1024**3:.1f} ГиБ")
    except (OSError, KeyError, TypeError, ValueError):
        return UNKNOWN, "свободное место не проверено"


ADB_RESTART_EVERY_S = 60.0
_adb_start_lock = threading.Lock()
_adb_started_at = 0.0


def adb_binary(cfg: Mapping[str, Mapping[str, Any]] | None = None) -> str | None:
    """Закреплённый бинарь adb ресурса с датчиком adb (поле `adb`), если он есть на диске."""
    cfg = config() if cfg is None else cfg
    for info in cfg.values():
        if str(info.get("sensor") or "") == "adb" and info.get("adb"):
            path = str(info["adb"])
            if os.path.isfile(path):
                return path
    return None


def ensure_adb_server(binary: str | None = None, *, wait_s: float = 6.0) -> bool:
    """Поднять демон adb вне Job Object вызывающего, если он не отвечает. Не чаще раза в минуту."""
    global _adb_started_at
    if _adb_devices(timeout=1.0)[0] != UNKNOWN:
        return True
    binary = binary or adb_binary()
    if not binary:
        return False
    with _adb_start_lock:
        if time.monotonic() - _adb_started_at < ADB_RESTART_EVERY_S:
            return False
        _adb_started_at = time.monotonic()
        import subprocess
        # 21.11 (№116 п. 7): без DETACHED_PROCESS. Вместе с ним CREATE_NO_WINDOW игнорируется (WinAPI),
        # процесс остаётся вовсе без консоли, и любой его консольный потомок получает ВИДИМОЕ окно.
        # NO_WINDOW даёт свою скрытую консоль, её наследуют потомки; вне Job Object выводит BREAKAWAY.
        base = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        for flags in (base | 0x01000000, base):  # CREATE_BREAKAWAY_FROM_JOB, затем без него
            try:
                subprocess.Popen([binary, "start-server"], creationflags=flags, close_fds=True,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                break
            except OSError:
                continue
        else:
            return False
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if _adb_devices(timeout=1.0)[0] != UNKNOWN:
            return True
        time.sleep(0.5)
    return False


def pin_adb(argv: list[str]) -> list[str]:
    """`run adb …` агента — закреплённым бинарём и при живом демоне вне задания команды."""
    if not argv or re.sub(r"\.exe$", "", os.path.basename(str(argv[0])).lower()) != "adb":
        return argv
    binary = adb_binary()
    if not binary:
        return argv
    ensure_adb_server(binary)
    return [binary, *argv[1:]]


ADB_FORBIDDEN = ("kill-server", "start-server", "reconnect", "-P", "-L")


def adb_refusal(argv: list[str]) -> str:
    """Команда агента, которая убила бы или подменила общий демон adb, — отказ с причиной."""
    if not argv or re.sub(r"\.exe$", "", os.path.basename(str(argv[0])).lower()) != "adb":
        return ""
    bad = [arg for arg in argv[1:] if arg in ADB_FORBIDDEN]
    if not bad:
        return ""
    return (f"adb {bad[0]} запрещён: демон adb общий и долгоживущий, его держит датчик телефона "
            "(05.11: демон, который убивали и поднимали команды агентов, обрывал телефон всем и "
            "заставлял владельца заново разрешать отладку). Нет устройства — это ресурс «телефон», "
            "а не повод перезапускать демон.")


def _adb_device_lines(host: str = "127.0.0.1", port: int = 5037,
                      timeout: float = 2.0) -> list[str] | None:
    """`host:devices-l`: строки `serial  state  product:… model:… device:…`; None — демон молчит."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            cmd = b"host:devices-l"
            sock.sendall(b"%04x" % len(cmd) + cmd)
            if _recv(sock, 4) != b"OKAY":
                return None
            size = int(_recv(sock, 4), 16)
            body = _recv(sock, size).decode("utf-8", "replace") if size else ""
    except (OSError, ValueError):
        return None
    return [" ".join(line.split()) for line in body.splitlines() if line.strip()]


def device_state(pattern: str, lines: list[str] | None) -> tuple[str, str]:
    """Состояние ресурса одного устройства по строкам `adb devices -l` (fail closed)."""
    try:
        rx = re.compile(pattern, re.I)
    except re.error as exc:
        return UNKNOWN, f"ошибка в device={pattern!r}: {exc}"
    if lines is None:
        return UNKNOWN, "adb-сервер недоступен"
    ready = [line for line in lines if len(line.split(" ")) > 1 and line.split(" ")[1] == "device"]
    mine = [line for line in ready if rx.search(line)]
    if mine:
        return UP, "устройство: " + "; ".join(line.split(" ")[0] for line in mine)
    seen = [line for line in lines if rx.search(line)]
    return DOWN, (f"нет готового устройства под {pattern!r}"
                  + (f" (видно: {'; '.join(seen)})" if seen else "")
                  + (f"; готовы другие: {', '.join(line.split(' ')[0] for line in ready)}"
                     if ready else ""))


def _adb_sensor(info: Mapping[str, Any]) -> tuple[str, str]:
    state = _adb_devices()
    if state[0] == UNKNOWN and info.get("adb") and ensure_adb_server(str(info["adb"])):
        state = _adb_devices()
    pattern = str(info.get("device") or "").strip()
    if pattern and state[0] != UNKNOWN:
        return device_state(pattern, _adb_device_lines())
    return state


SENSORS: dict[str, Callable[[Mapping[str, Any]], tuple[str, str]]] = {
    "adb": _adb_sensor,
    "tcp": _tcp,
    "http": _http,
    "disk": _disk,
}


def sense_all(cfg: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, tuple[str, str]]:
    cfg = config() if cfg is None else cfg
    out: dict[str, tuple[str, str]] = {}
    for name, info in cfg.items():
        fn = SENSORS.get(str(info.get("sensor") or ""))
        if fn is None:
            continue
        try:
            out[name] = fn(info)
        except Exception as exc:  # датчик не должен ронять обслуживание
            out[name] = (UNKNOWN, f"датчик упал: {type(exc).__name__}")
    return out


# ---- состояние в базе ---------------------------------------------------------------------

def last_state(tx: Any, name: str) -> tuple[str, str]:
    """(состояние, ts перехода). Нет записей — (unknown, '')."""
    row = tx.one(
        "SELECT ts,payload FROM events WHERE path_norm=? AND kind=? ORDER BY ts DESC LIMIT 1",
        (path_key(name), EVENT_KIND),
    )
    if row is None:
        return UNKNOWN, ""
    try:
        state = str(json.loads(str(row["payload"] or "{}")).get("state") or UNKNOWN)
    except ValueError:
        state = UNKNOWN
    return state, str(row["ts"])


def states(tx: Any, names: Iterable[str]) -> dict[str, tuple[str, str]]:
    return {name: last_state(tx, name) for name in names}


def down_resources(tx: Any, resources: Iterable[str], cfg: Mapping[str, Mapping[str, Any]] | None = None) -> list[str]:
    cfg = config() if cfg is None else cfg
    return [r for r in resources if cfg.get(r, {}).get("sensor") and last_state(tx, r)[0] == DOWN]


def _disk_hovering(info: Mapping[str, Any]) -> bool:
    """A disk sensor just above its minimum, within the recovery margin (default 1 GiB)."""
    if info.get("sensor") != "disk":
        return False
    import shutil
    try:
        free = shutil.disk_usage(str(info["path"])).free
        minimum = int(info.get("min_free_bytes", 8 * 1024 ** 3))
        margin = int(info.get("recover_margin_bytes", 1024 ** 3))
    except (OSError, KeyError, TypeError, ValueError):
        return False
    return free < minimum + margin


def record(store: Any, results: Mapping[str, tuple[str, str]]) -> list[str]:
    """Записать переходы. Возвращает имена ресурсов, ставших up."""
    came_up: list[str] = []
    cfg = config()
    with store.tx() as tx:
        for name, (state, detail) in results.items():
            prev, _ = last_state(tx, name)
            if prev == state:
                continue
            if prev == DOWN and state == UP and _disk_hovering(cfg.get(name) or {}):
                # 20.10.2026: build-disk flipped up/down every minute at the 8 GiB edge; each
                # "up" thaws reviews that the next build refuses again. Up needs a margin.
                continue
            tx.record_event(kind=EVENT_KIND, path_norm=path_key(name),
                            payload={"state": state, "was": prev, "detail": detail[:300]})
            if state == UP:
                came_up.append(name)
    return came_up


def tick(store: Any, *, every_s: float = SENSE_EVERY_S, sync: bool = False) -> list[str]:
    """Шаг обслуживания: записать готовый результат и, если пора, запустить опрос в фоне.

    Обслуживание идёт на каждом fleet_list, поэтому сам опрос — не чаще every_s и не в потоке
    вызова. `sync=True` — для тестов и пульта.
    """
    global _last_started, _running
    if os.environ.get("DEV_MCP_FLEET_SENSORS", "1") == "0":
        return []
    cfg = config()
    if not any(info.get("sensor") for info in cfg.values()):
        return []
    if sync:
        return record(store, sense_all(cfg))
    with _lock:
        ready = dict(_pending)
        _pending.clear()
        start = not _running and time.monotonic() - _last_started >= every_s
        if start:
            _running = True
            _last_started = time.monotonic()
    if start:
        def run() -> None:
            global _running
            try:
                res = sense_all(cfg)
                with _lock:
                    _pending.update(res)
            finally:
                with _lock:
                    _running = False
        threading.Thread(target=run, name="fleet-world-sense", daemon=True).start()
    return record(store, ready) if ready else []


# ---- зависимости: проверка мира не наследуется; уровни готовности ----------------------------
#
# Уровень готовности (07.10.2026). 75% рёбер needs не пересекаются по файлам: потребителю нужен
# контракт поставщика (схема, интерфейс, решение), а не вся его задача. `needs: ["TZ-12#contract"]`
# выполнено, когда TZ-12 принята, сдана, или её агент объявил уровень: `provide:contract` в
# fleet_checkpoint (событие `provide`). Поставщик дорабатывает дальше; сломал контракт — гейты
# потребителей это покажут.

PROVIDE_KIND = "provide"


_TASK_REF = re.compile(r"\b(?:(?:review|fix|request):)*(?:TZ-\d+[a-z]?|[0-9a-f]{8})(?::\d+)?\b", re.I)


def block_refs(db: Any, project: str | None = None) -> dict[tuple[str, str], list[str]]:
    """{(project, tz): задачи, названные в причине текущей блокировки} — для замороженных задач.

    Наследование мира (07.10.2026): TZ-61 Nova — код зелёный, «живой цикл ждёт TZ-78» — держала
    35 задач, хотя ни одной из них телефон не нужен. Задача, замороженная только из-за задач на
    ресурсе мира, сама становится задачей-доказательством в этом мире.
    """
    # 08.11: MAX(id) по индексу вместо сортировки всех событий задачи (на каждой выдаче задачи).
    sql = ("SELECT t.project,t.tz,(SELECT kind||''||payload FROM events WHERE id=(SELECT MAX(e.id) "
           "FROM events e WHERE e.project=t.project AND e.tz=t.tz "
           "AND e.kind IN ('block','freeze','thaw','reactivate','approve'))) AS last "
           "FROM tasks t WHERE t.state='frozen'")
    args: tuple[Any, ...] = ()
    if project:
        sql += " AND t.project=?"
        args = (project,)
    rows = db.query(sql, args) if hasattr(db, "query") else db.execute(sql, args).fetchall()
    out: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        kind, _, payload = str(row["last"] or "").partition("")
        if kind != "block":
            continue
        try:
            reason = str(json.loads(payload or "{}").get("reason") or "")
        except ValueError:
            continue
        refs: list[str] = []
        for m in _TASK_REF.finditer(reason):
            ref = m.group(0)
            if re.fullmatch(r"[0-9a-f]{8}", ref, re.I):
                ref = f"request:{ref}"
            if ref.upper() != str(row["tz"]).upper() and ref not in refs:
                refs.append(ref)
        if refs:
            out[(str(row["project"]), str(row["tz"]))] = refs
    return out


def dep_tz(dep: str) -> str:
    return str(dep).split("#", 1)[0].strip()


def dep_level(dep: str) -> str:
    parts = str(dep).split("#", 1)
    return parts[1].strip() if len(parts) == 2 else ""


def provided_levels(db: Any, project: str | None = None) -> dict[tuple[str, str], set[str]]:
    """{(project, tz): {уровни}} из событий provide. db — Txn (query) или sqlite3.Connection."""
    sql = "SELECT project,tz,payload FROM events WHERE kind=?"
    args: tuple[Any, ...] = (PROVIDE_KIND,)
    if project:
        sql += " AND project=?"
        args += (project,)
    rows = db.query(sql, args) if hasattr(db, "query") else db.execute(sql, args).fetchall()
    out: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        try:
            level = str(json.loads(str(row["payload"] or "{}")).get("level") or "")
        except ValueError:
            continue
        if level:
            out.setdefault((str(row["project"]), str(row["tz"])), set()).add(level)
    return out

def unmet_needs(
    project: str,
    needs: Iterable[str],
    own_resources: Iterable[str],
    index: Mapping[tuple[str, str], Mapping[str, Any]],
    world: frozenset[str],
) -> list[str]:
    """Непринятые зависимости, которые правда держат задачу.

    index[(project, tz)] = {"state", "needs", "resources"} — needs/resources списками.
    """
    mine = frozenset(own_resources) & world
    return [dep for dep in needs if not _satisfied(project, dep, mine, index, world, set())]


def effective_blockers(
    project: str,
    needs: Iterable[str],
    own_resources: Iterable[str],
    index: Mapping[tuple[str, str], Mapping[str, Any]],
    world: frozenset[str],
) -> list[str]:
    """Задачи, которые на самом деле держат эту: задача-доказательство в чужом мире заменяется
    теми своими входами, что держат её. Для пульта: «ворота» — настоящие, а не ближайшее звено.
    """
    mine = frozenset(own_resources) & world
    out: list[str] = []

    def walk(dep: str, path: frozenset[str]) -> None:
        tz = dep_tz(dep)
        if _satisfied(project, dep, mine, index, world, set()) or tz in path:
            return
        info = index.get((project, tz))
        relaxable = False
        if info is not None and info.get("state") not in ("accepted", "rejected"):
            dep_world = frozenset(info.get("resources") or ()) & world
            if info.get("state") == "frozen" and info.get("blocked_on"):
                refs = [index.get((project, r)) for r in info["blocked_on"]]
                refs = [r for r in refs if r is not None and r.get("state") != "accepted"]
                if refs and all(frozenset(r.get("resources") or ()) & world for r in refs):
                    dep_world |= frozenset().union(*(frozenset(r.get("resources") or ()) & world for r in refs))
            relaxable = bool(dep_world) and not (dep_world & mine)
        if not relaxable:
            if tz not in out:
                out.append(tz)
            return
        for inner in info.get("needs") or ():
            walk(inner, path | {tz})

    for dep in needs:
        walk(dep, frozenset())
    return out


def relaxed_needs(
    project: str,
    needs: Iterable[str],
    own_resources: Iterable[str],
    index: Mapping[tuple[str, str], Mapping[str, Any]],
    world: frozenset[str],
) -> list[str]:
    """Непринятые зависимости, которые НЕ держат задачу: живой прогон ещё впереди."""
    needs = list(needs)
    held = set(unmet_needs(project, needs, own_resources, index, world))
    return [d for d in needs
            if d not in held and index.get((project, dep_tz(d)), {}).get("state") != "accepted"]


def _satisfied(
    project: str, dep: str, mine: frozenset[str],
    index: Mapping[tuple[str, str], Mapping[str, Any]], world: frozenset[str], seen: set[str],
) -> bool:
    tz, level = dep_tz(dep), dep_level(dep)
    info = index.get((project, tz))
    if info is None:
        return False
    state = str(info.get("state") or "")
    if state == "accepted":
        return True
    if state == "rejected" or tz in seen:
        return False
    # #submitted is a lifecycle gate, never an early provide:* checkpoint.
    if level == "submitted":
        return state == "submitted"
    if level and (state == "submitted" or level in (info.get("provided") or ())):
        return True
    dep_world = frozenset(info.get("resources") or ()) & world
    if state == "frozen" and info.get("blocked_on"):
        # Заморожена только из-за задач в мире (TZ-61 ← TZ-78) — наследует их мир.
        inherited: set[str] = set()
        for ref in info["blocked_on"]:
            other = index.get((project, ref))
            if other is None or other.get("state") == "accepted":
                continue
            ref_world = frozenset(other.get("resources") or ()) & world
            if not ref_world:
                inherited = set()
                break
            inherited |= ref_world
        dep_world |= inherited
    if not dep_world or dep_world & mine:
        return False   # зависимость по коду, или потребитель сам работает в том же мире
    seen.add(tz)  # стек текущего пути (защита от цикла), а не «уже видели»: соседние ветки делят входы
    try:
        return all(_satisfied(project, d, mine, index, world, seen) for d in info.get("needs") or ())
    finally:
        seen.discard(tz)


def build_index(
    rows: Iterable[Mapping[str, Any]],
    provided: Mapping[tuple[str, str], set[str]] | None = None,
    blocked_on: Mapping[tuple[str, str], list[str]] | None = None,
) -> dict[tuple[str, str], dict[str, Any]]:
    def as_list(raw: Any) -> list[str]:
        if isinstance(raw, (list, tuple)):
            return [str(x) for x in raw]
        try:
            val = json.loads(str(raw or "[]"))
        except ValueError:
            return []
        return [str(x) for x in val] if isinstance(val, list) else []
    return {
        (str(r["project"]), str(r["tz"])): {
            "state": str(r["state"]), "needs": as_list(r["needs"]),
            "resources": as_list(r["resources"] if "resources" in r.keys() else "[]"),
            "provided": (provided or {}).get((str(r["project"]), str(r["tz"])), set()),
            "blocked_on": (blocked_on or {}).get((str(r["project"]), str(r["tz"])), []),
        }
        for r in rows
    }


# ---- ресурс на время процесса, а не захвата ---------------------------------------------------
#
# Зачем (07.10.2026). heavy-build — общий мьютекс сборок (14 ГБ памяти, две сборки не влезают). Его
# держал весь захват: Nova TZ-186 полтора часа думал и правил, а Beta App TZ-368 и Nova TZ-61
# стояли, хотя в захватах с heavy-build тяжёлых команд — меньшинство (build_app, build_local,
# candidate_bundle…; остальное — `py -c`, `adb shell`, `git diff`). Теперь такой ресурс
# (`scope = "run"`) не проверяется при выдаче: его берёт `run`, чья команда совпала с `run_match`,
# и держит до конца процесса — в том числе ушедшего в фон.

RUN_WAIT_S = 20.0
_run_locks: dict[str, threading.Lock] = {}
_run_holders: dict[str, str] = {}
_run_guard = threading.Lock()


def run_scoped(cfg: Mapping[str, Mapping[str, Any]] | None = None) -> frozenset[str]:
    cfg = config() if cfg is None else cfg
    return frozenset(name for name, info in cfg.items() if info.get("scope") == "run")


def _run_lock(name: str) -> threading.Lock:
    with _run_guard:
        return _run_locks.setdefault(name, threading.Lock())


def claim_resources(store: Any, worker_id: str) -> list[str]:
    if not worker_id:
        return []
    with store.read() as tx:
        rows = tx.query(
            "SELECT cr.resource FROM claim_resources cr JOIN claims c ON c.id=cr.claim_id "
            "WHERE c.worker_id=? AND c.state='active'", (worker_id,))
        return [str(r["resource"]) for r in rows]


# 21.11.2026 (№116 п. 8.4): run_match искал подстроку во ВСЕХ аргументах — `git diff build/x`,
# `rg bundle`, `adb shell … build.prop` и `product_update inspect` вставали в очередь за тяжёлой
# сборкой (heavy-build, build-disk). Аргументы этих программ — данные, а не команда сборки.
# Обёртки (cmd /c, powershell, py -c) по-прежнему сверяются целиком: неоднозначное — строго.
_DATA_ARG_EXES = frozenset({
    "git", "rg", "grep", "findstr", "where", "which", "type", "cat", "head", "tail", "ls", "dir",
    "adb", "fastboot", "certutil", "fc", "diff", "file", "wc", "sort",
})
_READ_ONLY_VERBS = frozenset({"inspect", "status", "list", "show", "help", "--help", "-h",
                              "--version", "version"})
_VERB_SCRIPT = re.compile(r"(?i)(?:^|[\\/.])(?:product_update|publish_update)(?:\.py)?$")


def run_match_text(argv: Iterable[str]) -> str:
    """Текст команды для сверки с run_match (пусто — команда не берёт ресурсы `scope = "run"`)."""
    args = [str(a) for a in argv]
    if not args:
        return ""
    exe = re.sub(r"(?i)\.(exe|cmd|bat|com)$", "", os.path.basename(args[0].strip('"'))).lower()
    if exe in _DATA_ARG_EXES:
        return ""
    for i, arg in enumerate(args[1:], 1):
        if _VERB_SCRIPT.search(arg):
            verb = next((a for a in args[i + 1:] if not a.startswith("-") or a in _READ_ONLY_VERBS), "")
            if verb.lower() in _READ_ONLY_VERBS:
                return ""
            break
    return " ".join(args)


def _run_needs(cfg: Mapping[str, Mapping[str, Any]], argv: Iterable[str],
               resources: Iterable[str] = ()) -> list[str]:
    scoped = run_scoped(cfg)
    names = set(resources) | {name for name in scoped if cfg[name].get("global") is True}
    text = run_match_text(argv)
    need: list[str] = []
    for name in sorted(names & scoped):
        pattern = str(cfg[name].get("run_match") or "")
        try:
            if not pattern or (text and re.search(pattern, text, re.IGNORECASE)):
                need.append(name)
        except re.error:
            need.append(name)  # invalid policy fails closed
    return need


ADB_READY = "adb-ready"


def is_adb_listing(argv: Iterable[str]) -> bool:
    """`adb devices [-l]` — rc=0 и без телефона: как проба препятствия он пуст.

    04.11–05.11 Nova VPN: fix:request:fc143e9d 10 раз за ночь — block «until: adb devices» →
    проба rc=0 → thaw → take → снова «нет устройства». Такую пробу сервер заменяет датчиком."""
    args = [str(a) for a in argv]
    if len(args) < 2 or re.sub(r"\.exe$", "", os.path.basename(args[0]).lower()) != "adb":
        return False
    rest = [a for a in args[1:] if a != "-l"]
    return rest == ["devices"]


def adb_ready(argv: list[str], cwd: str) -> int:
    """Проба `adb-ready`: 0 — у демона adb есть устройство в состоянии `device`. Клиента не запускает."""
    return 0 if _adb_devices()[0] == UP else 1


DISK_ADMISSION = "disk-admission"


def validate_disk_admission(argv: Iterable[str]) -> list[str]:
    args = list(argv)
    if (len(args) < 2 or args[0] != DISK_ADMISSION or
            sum(len(a) for a in args) > 8192 or
            any(not a or any(ord(c) < 32 for c in a) for a in args)):
        raise ValueError("ожидается disk-admission <исходная команда>")
    return args


def disk_admission(argv: list[str], cwd: str) -> int:
    """Read only: measure disk admission; never execute the embedded command.

    No claim or mutex is acquired. Admission is distinct from the task's own
    acceptance: config/capacity can change without any change in product code.
    The actual run still rechecks admission and takes all its normal locks.
    """
    args = validate_disk_admission(argv)
    cfg = config()
    return 0 if all(_disk(cfg[name])[0] == UP for name in _run_needs(cfg, args[1:])
                    if cfg[name].get("sensor") == "disk") else 1


def hold_for_run(store: Any, worker_id: str, argv: Iterable[str], who: str,
                 *, wait_s: float = RUN_WAIT_S) -> tuple[Callable[[], None] | None, str]:
    """Взять ресурсы `scope = "run"`, нужные этой команде. → (release или None, отказ).

    Отказ — не ошибка команды: ресурс занят чужой сборкой, агент повторит позже.
    """
    cfg = config()
    if not run_scoped(cfg):
        return (lambda: None), ""
    # Host-wide run resources also cover tasks whose specs forgot the resource.
    # A run permit is held until the process exits, including promotion to a job.
    need = _run_needs(cfg, argv, claim_resources(store, worker_id))
    taken: list[str] = []

    # Both fleet workers and quick/Claude operator runs use this path.
    # Lightweight commands remain allowed during emergency storage pressure.
    if {"heavy-build", "test-run"} & set(need):
        from .storage_guard import admission as storage_admission
        allowed, reason = storage_admission()
        if not allowed:
            return None, reason + ". Команда НЕ запущена."

    # Disk admission is a fresh, cheap host observation, outside any writer.
    # A stale UP event must not start another large job on a full volume.
    for name in need:
        if cfg[name].get("sensor") == "disk":
            state, detail = _disk(cfg[name])
            if state != UP:
                return None, (f"ресурс «{name}» недоступен: {detail}. Команда НЕ запущена. "
                              "Сервер проверяет место и сжимает старые резервные копии; "
                              "повтори run после восстановления ресурса. Если нужен fleet_block, "
                              "укажи независимую пробу until: disk-admission <исходная команда>.")

    def release() -> None:
        for name in taken:
            with _run_guard:
                _run_holders.pop(name, None)
            _run_lock(name).release()
        taken.clear()

    deadline = time.monotonic() + wait_s
    for name in sorted(need):
        slot, holder = _acquire_run_slot(name, cfg[name], who, deadline)
        if holder:
            release()
            return None, (
                f"ресурс «{name}» сейчас занят: {_holder_age(holder) if ';' not in holder else holder}. "
                "Команда НЕ запущена. ETA неизвестно: слот освободится после завершения процесса; "
                "повтори run позже. Это не ошибка задачи и не повод для fleet_block."
            )
        taken.append(slot)
    return release, ""


def command_resources(argv: Iterable[str]) -> list[str]:
    """Resources of a finite command, independent of an agent's task declaration."""
    return _run_needs(config(), argv)


# 17.10.2026 (сессия Beta App): мьютекс жил только в памяти процесса dev-mcp и только на
# пути `run`. Пять check_all шли параллельно (0,7 ГБ свободно из 14): гейты интеграции идут
# через exec_preflight.run_command мимо него, product_release_check не попадал в run_match
# (check_all он порождает сам), а после рестарта dev-mcp осиротевшие прогоны не учитывались.
# Теперь занятость — это ещё и живой чужой python-процесс с той же командой (не потомок нас:
# свои прогоны и так держат замок), и гейты берут тот же замок через hold_blocking.

def _scan_pattern(info: Mapping[str, Any]) -> str:
    """Чужие процессы ищем только у ресурсов с scan_processes = true (heavy-build): pytest других
    сессий не должен держать гейты флота."""
    return str(info.get("run_match") or "") if info.get("scan_processes") is True else ""


_SCAN_EXES = ("python.exe", "pythonw.exe", "py.exe", "python", "python3")


def _foreign_runs(pattern: str) -> list[str]:
    """Живые python-процессы вне нашего дерева, чья команда подходит под run_match."""
    if not pattern:
        return []
    try:
        import psutil
    except ImportError:
        return []
    try:
        me = psutil.Process()
        own = {me.pid, *(c.pid for c in me.children(recursive=True))}
    except psutil.Error:
        own = {os.getpid()}
    found: list[str] = []
    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            info = proc.info
            if info["pid"] in own or str(info["name"] or "").lower() not in _SCAN_EXES:
                continue
            text = " ".join(info["cmdline"] or ())
            if text and re.search(pattern, text, re.IGNORECASE):
                started = time.strftime("%H:%M", time.localtime(info["create_time"] or 0))
                found.append(f"pid {info['pid']} с {started}: {text[:90]}")
        except (psutil.Error, re.error):
            continue
    return found


def _acquire_run(name: str, pattern: str, who: str, deadline: float) -> str:
    """Взять замок ресурса до deadline. → '' если взят, иначе кто держит."""
    lock = _run_lock(name)
    while True:
        if lock.acquire(timeout=max(0.0, min(5.0, deadline - time.monotonic()))):
            foreign = _foreign_runs(pattern)
            if not foreign:
                with _run_guard:
                    _run_holders[name] = f"{who} с {time.strftime('%H:%M:%S')}"
                return ""
            lock.release()
            holder = "вне учёта dev-mcp — " + "; ".join(foreign[:3])
        else:
            holder = _run_holders.get(name, "?")
        if time.monotonic() >= deadline:
            return holder
        time.sleep(min(5.0, max(0.0, deadline - time.monotonic())))


def _holder_age(holder: str) -> str:
    """Elapsed wall-clock time; completion time is not predictable."""
    match = re.search(r' с (\d{2}:\d{2}:\d{2})$', holder)
    if not match:
        return holder + ' (время удержания неизвестно)'
    try:
        old = time.strptime(match.group(1), '%H:%M:%S')
        now = time.localtime()
        seconds = ((now.tm_hour - old.tm_hour) * 3600 +
                   (now.tm_min - old.tm_min) * 60 + now.tm_sec - old.tm_sec) % 86400
        return f'{holder}, удерживает {seconds // 60} мин'
    except ValueError:
        return holder


def _acquire_run_slot(name: str, info: Mapping[str, Any], who: str,
                      deadline: float) -> tuple[str, str]:
    """Independent slots for lightweight pytest, with a RAM-aware second permit."""
    capacity = max(1, min(4, int(info.get('capacity', 1))))
    if capacity > 1:
        try:
            import psutil
            if psutil.virtual_memory().available < 2 * 1024 ** 3:
                capacity = 1
        except (ImportError, OSError):
            capacity = 1
    if capacity == 1:
        if int(info.get('capacity', 1)) > 1:
            # RAM may fall below 2 GiB while a second slot is already live.
            # Do not admit another run through free slot #0 in that case.
            with _run_guard:
                other = [f'{slot}: {_holder_age(holder)}' for slot, holder in _run_holders.items()
                         if slot.startswith(name + '#') and slot != name + '#0']
            if other:
                return '', '; '.join(other)
        holder = _acquire_run(name + '#0' if info.get('capacity', 1) != 1 else name,
                              _scan_pattern(info), who, deadline)
        return (name + '#0' if info.get('capacity', 1) != 1 else name, holder)
    slots = [f'{name}#{i}' for i in range(capacity)]
    pattern = _scan_pattern(info)
    while True:
        # 25.11 (ночное ревью, HIGH): как и при capacity=1, живые чужие прогоны (scan_processes)
        # занимают слоты — иначе внешний heavy-процесс не уменьшал число допусков.
        foreign = _foreign_runs(pattern) if pattern else []
        with _run_guard:
            held = sum(1 for slot in slots if slot in _run_holders)
        if held + len(foreign) < capacity:
            for slot in slots:
                lock = _run_lock(slot)
                if lock.acquire(blocking=False):
                    with _run_guard:
                        _run_holders[slot] = f"{who} с {time.strftime('%H:%M:%S')}"
                    return slot, ''
        if time.monotonic() >= deadline:
            with _run_guard:
                holders = [f'{slot}: {_holder_age(_run_holders.get(slot, "?"))}' for slot in slots]
            if foreign:
                holders.append("вне учёта dev-mcp — " + "; ".join(foreign[:3]))
            return '', '; '.join(holders)
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))


def hold_blocking(argv: Iterable[str], who: str, wait_s: float) -> tuple[Callable[[], None] | None, str]:
    """Тот же замок для гейтов и приёмок интегратора (без захвата агента): ждать до wait_s."""
    if os.environ.get("DEV_MCP_FLEET_GATE_LOCK", "1") == "0":
        return (lambda: None), ""
    cfg = config()
    need = _run_needs(cfg, argv)
    if {"heavy-build", "test-run"} & set(need):
        from .storage_guard import admission as storage_admission
        allowed, reason = storage_admission()
        if not allowed:
            return None, reason + ". Команда НЕ запущена."
    taken: list[str] = []

    def release() -> None:
        for name in taken:
            with _run_guard:
                _run_holders.pop(name, None)
            _run_lock(name).release()
        taken.clear()

    deadline = time.monotonic() + wait_s
    for name in sorted(need):
        if cfg[name].get("sensor") == "disk":
            state,detail=_disk(cfg[name])
            if state!=UP:
                release()
                return None,f'ресурс «{name}» недоступен: {detail}'
            continue
        slot, holder = _acquire_run_slot(name, cfg[name], who, deadline)
        if holder:
            release()
            return None, f"ресурс «{name}» занят: {holder}"
        taken.append(slot)
    return release, ""


def command_available(argv: Iterable[str]) -> bool:
    """A cheap admission probe. Execution must still acquire its own permit."""
    release,busy=hold_blocking(argv,'admission probe',wait_s=0)
    if release is None:
        return False
    release()
    return not busy


BUSY_TTL_S = 5.0
_busy_cache: dict[str, Any] = {"at": -1e9, "busy": {}}


def busy_run_resources(cfg: Mapping[str, Mapping[str, Any]] | None = None, *,
                       ttl_s: float = BUSY_TTL_S) -> dict[str, str]:
    """Ресурсы `scope = "run"`, занятые прямо сейчас: имя → кто держит / почему недоступен.

    Наблюдение для допуска к выдаче, не разрешение на запуск: сам `run` берёт замок заново.
    03.11.2026: ревью Beta App выдавались, пока `heavy-build` держал операторский
    check_all; ревьюер блокировался, задача оттаивала и выдавалась снова — 416 взятий
    за 4,5 ч. Занятость, известная до выдачи, не должна становиться захватом.
    """
    if os.environ.get("DEV_MCP_FLEET_GATE_LOCK", "1") == "0":
        return {}
    now = time.monotonic()
    if cfg is None and now - float(_busy_cache["at"]) < ttl_s:
        return dict(_busy_cache["busy"])
    use = config() if cfg is None else cfg
    busy: dict[str, str] = {}
    for name in sorted(run_scoped(use)):
        info = use[name]
        if info.get("sensor") == "disk":
            state, detail = _disk(info)
            if state != UP:
                busy[name] = f"недоступен: {detail}"
            continue
        with _run_guard:
            holder = _run_holders.get(name, "")
            if int(info.get('capacity', 1)) > 1:
                slots = [f'{name}#{i}' for i in range(min(4, int(info['capacity'])))]
                occupied = [_run_holders.get(slot, '') for slot in slots]
                if all(occupied):
                    holder = '; '.join(occupied)
                else:
                    holder = ''
        if not holder:
            foreign = _foreign_runs(_scan_pattern(info))
            if foreign:
                holder = "вне учёта dev-mcp — " + "; ".join(foreign[:2])
        if holder:
            busy[name] = holder
    if cfg is None:
        _busy_cache.update(at=now, busy=dict(busy))
    return busy


def acceptance_run_needs(acceptance: Iterable[str],
                         cfg: Mapping[str, Mapping[str, Any]] | None = None) -> list[str]:
    """Ресурсы `scope = "run"`, без которых не запустится хотя бы одна команда приёмки."""
    from .exec_preflight import parse_command
    use = config() if cfg is None else cfg
    need: set[str] = set()
    for command in acceptance:
        try:
            argv = parse_command(str(command))
        except (ValueError, TypeError):
            continue
        need.update(_run_needs(use, argv))
    return sorted(need)


def run_resource_snapshot() -> tuple:
    """Wake signal only; does not acquire, release or authorize execution."""
    cfg=config()
    with _run_guard:
        owned=tuple(sorted(_run_holders.items()))
    foreign=[]
    for name,info in sorted(cfg.items()):
        pattern=_scan_pattern(info)
        if pattern:
            foreign.append((name,tuple(_foreign_runs(pattern))))
    return owned,tuple(foreign)


OWNER_NEED_AFTER_S = 15 * 60


def owner_needs(store: Any, *, now: datetime | None = None,
                after_s: float = OWNER_NEED_AFTER_S) -> list[dict[str, Any]]:
    """Ресурс мира, который нужен работе и давно лежит, — то, что может сделать только владелец.

    05.11.2026: «подключи PHONEB» владелец узнавал от оператора в чате, а не от системы. Условие
    уровневое: датчик down дольше after_s (телефон моргает десятки раз в сутки — моргание не
    повод звать человека) и есть живые задачи с этим ресурсом. Текст — свежий ответ датчика,
    а не запись перехода: запись могла устареть или быть догадкой.
    """
    now = now or datetime.now(timezone.utc)
    cfg = config()
    out: list[dict[str, Any]] = []
    with store.read() as tx:
        for name, info in sorted(cfg.items()):
            sensor = info.get("sensor")
            if not sensor:
                continue
            state, since = last_state(tx, name)
            if state != DOWN:
                continue
            try:
                at = datetime.fromisoformat(since.replace(" ", "T"))
                at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if (now - at).total_seconds() < after_s:
                continue
            waiting = [(str(r["project"]), str(r["tz"])) for r in tx.query(
                "SELECT t.project,t.tz FROM tasks t, json_each(t.resources) j "
                "WHERE j.value=? AND t.state IN ('ready','frozen','taken') ORDER BY t.project,t.tz",
                (name,))]
            if not waiting:
                continue
            out.append({"resource": name, "since": at.isoformat(), "waiting": waiting,
                        "need": str(info.get("need") or "")})
    for item in out:
        probe = SENSORS.get(str(cfg[item["resource"]].get("sensor")))
        try:
            state, detail = probe(cfg[item["resource"]]) if probe else (DOWN, "")
        except Exception as exc:  # noqa: BLE001 — датчик не должен ронять доску
            state, detail = DOWN, f"датчик не ответил ({type(exc).__name__})"
        item["detail"], item["live"] = detail, state
    return [i for i in out if i["live"] != UP]
