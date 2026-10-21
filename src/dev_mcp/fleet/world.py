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

Датчик `adb` не запускает клиент adb: на машине их несколько разных версий, а клиент чужой
версии убивает демон — и обрывает агентам живую сессию с телефоном. Демон не запущен — состояние
`unknown`: ничего не запрещаем, работает старое поведение (агент сам запустит демон).
"""
from __future__ import annotations

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
        "($_.FriendlyName -match 'ADB|Android|MTP|Composite|TECNO|Portable|MediaTek|MTK') } | "
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
    if names:
        text = ("Windows видит USB: " + ", ".join(names[:3])
                + " — но adb пуст (отладка по USB / разрешение на телефоне)")
    else:
        text = "Windows не видит телефон по USB вовсе (кабель «только зарядка», порт или режим на телефоне)"
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


SENSORS: dict[str, Callable[[Mapping[str, Any]], tuple[str, str]]] = {
    "adb": lambda info: _adb_devices(),
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
    sql = ("SELECT t.project,t.tz,(SELECT kind||''||payload FROM events e WHERE e.project=t.project "
           "AND e.tz=t.tz AND e.kind IN ('block','freeze','thaw','reactivate','approve') "
           "ORDER BY e.id DESC LIMIT 1) AS last FROM tasks t WHERE t.state='frozen'")
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


def _run_needs(cfg: Mapping[str, Mapping[str, Any]], argv: Iterable[str],
               resources: Iterable[str] = ()) -> list[str]:
    import re
    scoped = run_scoped(cfg)
    names = set(resources) | {name for name in scoped if cfg[name].get("global") is True}
    text = " ".join(str(a) for a in argv)
    need: list[str] = []
    for name in sorted(names & scoped):
        pattern = str(cfg[name].get("run_match") or "")
        try:
            if not pattern or re.search(pattern, text, re.IGNORECASE):
                need.append(name)
        except re.error:
            need.append(name)  # invalid policy fails closed
    return need


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
        holder = _acquire_run(name, _scan_pattern(cfg[name]), who, deadline)
        if holder:
            release()
            return None, (
                f"ресурс «{name}» сейчас занят: {holder}. Команда НЕ запущена. Сделай пока то, что "
                f"не требует «{name}», и повтори run позже — это не ошибка задачи и не повод для "
                "fleet_block."
            )
        taken.append(name)
    return release, ""


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


def hold_blocking(argv: Iterable[str], who: str, wait_s: float) -> tuple[Callable[[], None] | None, str]:
    """Тот же замок для гейтов и приёмок интегратора (без захвата агента): ждать до wait_s."""
    if os.environ.get("DEV_MCP_FLEET_GATE_LOCK", "1") == "0":
        return (lambda: None), ""
    cfg = config()
    need = _run_needs(cfg, argv)
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
            continue
        holder = _acquire_run(name, _scan_pattern(cfg[name]), who, deadline)
        if holder:
            release()
            return None, f"ресурс «{name}» занят: {holder}"
        taken.append(name)
    return release, ""
