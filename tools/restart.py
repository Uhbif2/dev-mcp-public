"""Одна кнопка: проверить policy.toml и перезапустить сервис dev-mcp.

Что делает по шагам:
  1. проверяет policy.toml (синтаксис + боевые стражи);
  2. перезапускает НУЖНЫЙ экземпляр mcp-proxy (по умолчанию только dev на :8766),
     лончер поднимает его сам; если лончера нет — запускает лончер;
  3. проверяет локальный хоп  http://localhost:8766/servers/dev/mcp;
  4. проверяет воркер шлюза (poll_worker.py) и, если он лежит, поднимает его;
  5. проверяет публичный хоп через шлюз Oracle (то, что делает ChatGPT).

Флаги:  --core         перезапустить core-экземпляр (:8765: memory_service, obsidian, …)
        --all          перезапустить оба экземпляра (и лончер целиком)
        --instance N   только экземпляр N (aux :8767, lib :8768 — живая библиотека)
        --local-only   только шаги 1–3 (не трогать воркер и шлюз)
        --no-tests     пропустить боевые стражи (шаг 1 оставляет только синтаксис)
        --force-tests  прогнать стражей, даже если код и конфиги не менялись

21.11.2026 — быстрее и без «зависаний».

* **Стражи не гоняются зря.** Отпечаток (sha256) policy.toml, fleet-projects.toml,
  pyproject.toml, src/, tests/ и самого restart.py сравнивается с последним зелёным
  прогоном (logs/restart-guards.ok.json, не старше 24 ч). Совпал — шаг 1 занимает доли
  секунды. Иначе pytest получает только файлы, где встречается «production» (отбор тот же).
* **Один снимок процессов вместо пачки PowerShell/WMI.** Раньше до taskkill шло 3–5
  запусков PowerShell с Get-CimInstance (до 30 с каждый при нехватке RAM, без перехвата
  таймаута). Теперь — psutil, если есть, иначе один вызов WMI; порты — netstat.
  Не удалось получить список процессов — ошибка, а не второй лончер.
* **Каждый шаг печатает прошедшее время**, так что зависание видно сразу.
* **Шаг 2 ждёт по стадиям, а не 2 минуты.** При нехватке RAM (21.11: 0,1 ГиБ свободно) dev
  поднимался 13 мин: прокси 78 с, dev-mcp до server_start 8 мин, uvicorn ещё 3 мин. Старый
  предел в 2 мин рисовал [X], и следующий restart.cmd убивал уже поднявшийся сервер. Теперь
  стадии читаются из mcp-proxy\\mcp_proxy_<имя>.log; ошибка — только если 5 мин нет движения,
  экземпляр упал или прошло 20 мин. Экземпляр, который уже поднимается после последней правки
  кода, повторный запуск не трогает, а дожидается. В начале — предупреждение о свободной RAM.

19.09.2026 — что здесь поменялось и почему.

* **Перезапуск стал точечным.** Раньше скрипт убивал лончер и ВЕСЬ mcp-proxy со
  всеми named-server и ждал порт до 5 минут (холодный старт тащил за собой npm за
  mcp-remote, memory_service, Neo4j). Теперь dev-mcp живёт отдельным процессом на :8766
  (см. Gateway Host\\mcp-proxy\\mcp_launcher.py), поэтому правка кода dev-mcp
  перезапускает ровно его — секунды вместо минут, и memory_service/obsidian при этом не
  моргают. `--core` и `--all` оставлены для тех случаев, когда надо иначе.
* **Скрипт больше не зовёт orchestrator.ps1 вслепую.** Тот поднимает элемент, если
  его «порт мёртв», — а мёртвый порт во время крэш-лупа означал ВТОРОЙ лончер и
  конфликт за порт (WinError 10048), то есть скрипт своей же диагностикой добивал
  то, что чинил. Теперь воркер поднимается напрямую своей обёрткой, а лончер — по
  факту отсутствия процесса.
* **Итог пишется в logs\\restart-last.txt.** Скрипт часто запускает сам агент через
  dev-mcp, и его собственный вызов обрывается ровно в момент перезапуска dev —
  это нормально и ожидаемо. Отчёт в файле позволяет агенту (и владельцу) узнать
  результат после того, как соединение восстановилось.

Запускать через restart.cmd (двойной клик) либо:
    .venv\\Scripts\\python.exe tools\\restart.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# Этот скрипт проверяет реальную доступность localhost и VPS-шлюза — системный
# HTTP_PROXY/HTTPS_PROXY/ALL_PROXY (настроен для других инструментов, напр. CRM)
# не должен участвовать: urllib.request по умолчанию молча его уважает, из-за
# чего шаги 4-5 могут висеть на таймауте, даже когда шлюз отвечает мгновенно
# напрямую. Ставим глобальный no-proxy opener для всех urlopen() в этом файле.
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

# `restart.py` можно запускать и напрямую, минуя restart.cmd. Диагностика и
# запущенный ею launcher должны видеть тот же стабильный PATH, что и run.cmd;
# иначе wrapper зелёный, а прямой запуск сообщает другой набор missing-bin.
_EXEC_PATH_DIRS = (
    Path.home() / "memory_service" / ".venv" / "Scripts",
    Path.home() / "platform-tools",
)
_path_parts = os.environ.get("PATH", "").split(os.pathsep)
_path_keys = {os.path.normcase(os.path.normpath(part)) for part in _path_parts if part}
for _exec_dir in _EXEC_PATH_DIRS:
    _key = os.path.normcase(os.path.normpath(str(_exec_dir)))
    if _exec_dir.is_dir() and _key not in _path_keys:
        _path_parts.append(str(_exec_dir))
        _path_keys.add(_key)
os.environ["PATH"] = os.pathsep.join(_path_parts)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from dev_mcp.policy import Policy, PolicyError  # noqa: E402
from dev_mcp import machine  # noqa: E402

# 08.10.2026 проект Oracle переезжает в пульт; старый путь — пока переезд не завершён.
OCVPS = machine.PROXY_STATE.parent.parent
LAUNCHER = OCVPS / "mcp-proxy" / "mcp_launcher.py"
PROXY_STATE = OCVPS / "mcp-proxy" / "state.json"
GATEWAY_ENV = OCVPS / "gateway" / "gateway.env"
WORKER_ENV = OCVPS / "worker" / "worker.env"
WORKER_VBS = OCVPS / "worker" / "run-worker-hidden.vbs"
PYTHONW = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python" / "Python313" / "pythonw.exe"
GATEWAY_URL_FALLBACK = "https://gateway.example.net"
REPORT = REPO / "logs" / "restart-last.txt"
GUARD_TIMEOUT_S = 900

# Экземпляры mcp-proxy. Должно совпадать с INSTANCES в mcp_launcher.py.
INSTANCES = {
    "dev": {"port": 8766, "probe": "dev", "what": "dev-mcp (флот агентов)"},
    "core": {"port": 8765, "probe": "memory_service", "what": "memory_service, obsidian, google-sheets, demo-service"},
    "aux": {"port": 8767, "probe": "tg-notify", "what": "soft-patterns, tg-notify, second-pc"},
    "lib": {"port": 8768, "probe": "local-library", "what": "local-library (живая библиотека)"},
}

INIT_BODY = json.dumps({
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "restart.py", "version": "0"}},
}).encode()

_ok = True
_public_ok = False
_lines: list[str] = []
_T0 = time.monotonic()
GUARD_STAMP = REPO / "logs" / "restart-guards.ok.json"
GUARD_LOG = REPO / "logs" / "restart-guards.log"
GUARD_STAMP_MAX_AGE_S = 24 * 3600
START_LIMIT_S = 20 * 60      # общий предел ожидания порта
START_STALL_S = 5 * 60       # столько без движения в журнале прокси — ошибка
_STAGES = (("child", "Setting up named server"), ("server", "Starting MCP server"),
           ("up", "Uvicorn running"))
_STAGE_TEXT = {
    "proxy": "mcp-proxy запускается",
    "child": "dev-mcp запущен (run.cmd): импорт и загрузка политики",
    "server": "dev-mcp готов, mcp-proxy поднимает HTTP",
    "up": "uvicorn запущен",
    "crashed": "экземпляр упал, лончер перезапускает",
}


def say(mark: str, msg: str) -> None:
    _lines.append(f"  {mark} {msg}")
    print(f"  {mark} {msg}", flush=True)


def head(msg: str) -> None:
    if msg.strip() and not msg.strip().startswith("="):
        msg = f"{msg}   [+{time.monotonic() - _T0:.0f} с]"
    _lines.append(msg)
    print(msg, flush=True)


def fail(msg: str) -> None:
    global _ok
    _ok = False
    say("[X]", msg)


def write_report() -> None:
    """Отчёт на диск: свой же вызов агент теряет ровно в момент перезапуска dev."""
    try:
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(
            f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {'ГОТОВО' if _ok else 'С ОШИБКАМИ'}\n"
            + "\n".join(_lines) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def _env_value(path: Path, key: str) -> str:
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _ps(cmd: str, timeout: int = 30) -> str:
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                           capture_output=True, text=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return (r.stdout or "").strip()


# Один снимок процессов на шаг вместо PowerShell/WMI на каждый вопрос (21.11.2026).
_TABLE: dict = {"at": 0.0, "rows": None}
_PY_NAME = re.compile(r"(?i)^(?:python|pythonw|py-[^\\/]*)\.exe$")


def _invalidate_table() -> None:
    _TABLE.update(at=0.0, rows=None, full=False)


def _process_table(max_age: float = 15.0) -> list[dict] | None:
    """[{pid, name, cmd}] или None, если список процессов получить не удалось."""
    if _TABLE["rows"] is not None and time.monotonic() - _TABLE["at"] < max_age:
        return _TABLE["rows"]
    rows: list[dict] | None = None
    full = False
    try:
        import psutil  # type: ignore[import-not-found]
        rows = []
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                cmd = " ".join(proc.cmdline())
            except Exception:  # noqa: BLE001 - AccessDenied/ZombieProcess
                cmd = ""
            rows.append({"pid": int(proc.info["pid"]), "name": str(proc.info.get("name") or ""), "cmd": cmd})
        full = True
    except ImportError:
        rows = None
    except Exception:  # noqa: BLE001
        rows = None
    if rows is None:
        out = _ps("Get-CimInstance Win32_Process -Filter \"Name LIKE 'py%' OR Name='wscript.exe'\" | "
                  "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress", timeout=90)
        try:
            data = json.loads(out) if out else None
        except ValueError:
            data = None
        if isinstance(data, dict):
            data = [data]
        if isinstance(data, list) and data:
            rows = [{"pid": int(d.get("ProcessId") or 0), "name": str(d.get("Name") or ""),
                     "cmd": str(d.get("CommandLine") or "")} for d in data if isinstance(d, dict)]
    if rows is not None:
        _TABLE.update(at=time.monotonic(), rows=rows, full=full)
    return rows


def _split_literal(needle: str) -> str:
    """PowerShell-выражение, собирающее строку из двух половин.

    Нужно ровно затем, чтобы искомая подстрока НЕ встречалась в командной строке
    самого запроса. Иначе `Get-CimInstance | ? { $_.CommandLine -like '*X*' }`
    находит… собственный powershell.exe и отвечает «процесс жив» всегда. Так
    проверка воркера здесь молча возвращала True при мёртвом воркере — а значит
    не поднимала его ни разу.
    """
    cut = max(1, len(needle) // 2)
    return f"'{needle[:cut]}' + '{needle[cut:]}'"


def _pids_of(*needles: str) -> list[int]:
    """PID python-процессов с подстрокой в командной строке; RuntimeError — список недоступен."""
    rows = _process_table()
    if rows is None:
        raise RuntimeError("список процессов недоступен (psutil нет, PowerShell/WMI не ответили)")
    me = os.getpid()
    return [r["pid"] for r in rows if r["pid"] != me and _PY_NAME.match(r["name"])
            and any(n.casefold() in r["cmd"].casefold() for n in needles)]


def _proc_alive(needle: str) -> bool:
    rows = _process_table()
    if rows is not None:
        me = os.getpid()
        return any(r["pid"] != me and needle.casefold() in r["cmd"].casefold() for r in rows)
    return _ps(f"$n = {_split_literal(needle)}; "
               f"[bool](Get-CimInstance Win32_Process | ? {{ $_.CommandLine -like \"*$n*\" }})") == "True"


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _mcp_initialize(url: str, timeout: int = 30) -> tuple[bool, str]:
    req = urllib.request.Request(url, data=INIT_BODY, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            body = "\n".join(l[5:].strip() for l in raw.splitlines() if l.startswith("data:")) or raw
            name = json.loads(body).get("result", {}).get("serverInfo", {}).get("name", "?")
            return (r.status == 200), f"HTTP {r.status}, serverInfo={name}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------

def _guard_fingerprint() -> str:
    import hashlib
    digest = hashlib.sha256()
    files = [REPO / "policy.toml", REPO / "fleet-projects.toml", REPO / "pyproject.toml",
             Path(__file__).resolve()]
    for sub in ("src", "tests"):
        files += sorted(p for p in (REPO / sub).rglob("*.py") if "__pycache__" not in p.parts)
    for path in files:
        try:
            data = path.read_bytes()
        except OSError:
            data = b"<missing>"
        digest.update(str(path.relative_to(REPO) if path.is_relative_to(REPO) else path.name).encode())
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


def _guard_files() -> list[str]:
    """Файлы, где вообще встречается «production»: -k production выбирает из них то же самое."""
    picked = []
    for path in sorted((REPO / "tests").rglob("test_*.py")):
        try:
            if "production" in path.name or "production" in path.read_text(encoding="utf-8", errors="ignore"):
                picked.append(str(path.relative_to(REPO)))
        except OSError:
            return ["tests"]
    return picked or ["tests"]


def _guard_cached(fingerprint: str) -> dict | None:
    try:
        stamp = json.loads(GUARD_STAMP.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (isinstance(stamp, dict) and stamp.get("fingerprint") == fingerprint
            and time.time() - float(stamp.get("at") or 0) < GUARD_STAMP_MAX_AGE_S):
        return stamp
    return None


def step_check_policy(run_tests: bool, force_tests: bool = False) -> Policy | None:
    head("\n[1/5] Проверка policy.toml")
    try:
        policy = Policy.load(str(REPO / "policy.toml"))
    except (PolicyError, OSError) as exc:
        fail(f"policy.toml не загружается:\n      {exc}")
        return None
    say("[OK]", f"синтаксис ок — корней {len(policy.roots)}, exec_roots {len(policy.exec_roots)}, "
                f"бинарей {len(policy.exec_allowed_bin)}")
    if policy._exec_bin_missing:
        say("[!]", f"нет в PATH (run ответит отказом): {', '.join(policy._exec_bin_missing)}")
    fingerprint = _guard_fingerprint() if run_tests else ""
    cached = None if (force_tests or not run_tests) else _guard_cached(fingerprint)
    if cached:
        age = (time.time() - float(cached["at"])) / 60
        say("[OK]", f"боевые стражи: код, тесты и конфиги не менялись с зелёного прогона "
                    f"{age:.0f} мин назад ({cached.get('last', '')[:80]}) — прогон пропущен; "
                    f"--force-tests прогонит заново")
        return policy
    if run_tests:
        # 09.11.2026: the step hung with no output. A live log plus a hard limit; on timeout
        # name the last started test instead of waiting forever.
        guard_log = GUARD_LOG
        guard_log.parent.mkdir(exist_ok=True)
        with guard_log.open("w", encoding="utf-8", errors="replace") as fh:
            try:
                r = subprocess.run([sys.executable, "-m", "pytest", *_guard_files(), "-v", "-k", "production",
                                    "--no-header", "-p", "no:cacheprovider"],
                                   cwd=str(REPO), stdout=fh, stderr=subprocess.STDOUT, text=True,
                                   timeout=GUARD_TIMEOUT_S,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except subprocess.TimeoutExpired:
                tail = guard_log.read_text(encoding="utf-8", errors="replace").strip().splitlines()
                fail(f"guards exceeded {GUARD_TIMEOUT_S}s; last test: {(tail or ['?'])[-1][:200]}; log: {guard_log}")
                return None
        lines = guard_log.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        last = lines[-1] if lines else ""
        if r.returncode == 0:
            say("[OK]", f"боевые стражи: {last}")
            try:
                GUARD_STAMP.write_text(json.dumps({"fingerprint": fingerprint, "at": time.time(),
                                                   "last": last}, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
        else:
            fail(f"боевые стражи упали (config ослаблен?):\n      {last}\n"
                 f"      подробно: .venv\\Scripts\\python.exe -m pytest tests -k production")
            return None
    return policy


def _ensure_launcher() -> bool:
    """Лончер должен быть ровно один. Нет — поднять; есть — не трогать.

    Второй лончер — это два mcp-proxy на один порт и вечный WinError 10048;
    поэтому «на всякий случай перезапустить» здесь неправильный рефлекс.
    """
    try:
        if _pids_of("mcp_launcher.py"):
            return True
    except RuntimeError as exc:
        fail(f"{exc} — лончер не трогаю, перезапуск остановлен (повтори через минуту)")
        return False
    if not LAUNCHER.exists():
        fail(f"лончер не найден: {LAUNCHER}")
        return False
    if not PYTHONW.exists():
        fail(f"pythonw.exe не найден: {PYTHONW}")
        return False
    subprocess.Popen([str(PYTHONW), str(LAUNCHER)], cwd=str(LAUNCHER.parent),
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    say("[..]", "лончера не было — запустил")
    return True


def _free_ram_gib() -> float | None:
    if os.name != "nt":
        return None
    import ctypes

    class _Mem(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                    ("avail", ctypes.c_ulonglong), ("pt", ctypes.c_ulonglong), ("pa", ctypes.c_ulonglong),
                    ("vt", ctypes.c_ulonglong), ("va", ctypes.c_ulonglong), ("ve", ctypes.c_ulonglong)]
    mem = _Mem()
    mem.length = ctypes.sizeof(_Mem)
    try:
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
            return None
    except Exception:  # noqa: BLE001
        return None
    return mem.avail / 2**30


def _start_progress(name: str) -> dict:
    """Стадия последнего старта экземпляра по его журналу mcp-proxy (без процессов и WMI)."""
    st = _proxy_state().get(name, {})
    path = PROXY_STATE.parent / str((st or {}).get("log") or f"mcp_proxy_{name}.log")
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - 400_000))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return {"stage": "", "size": 0, "started": None}
    marker = f"=== mcp-proxy[{name}] START "
    at = text.rfind(marker)
    if at < 0:
        return {"stage": "", "size": size, "started": None}
    segment = text[at:]
    started = None
    match = re.match(r"=== mcp-proxy\[[^\]]+\] START (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", segment)
    if match:
        try:
            started = time.mktime(time.strptime(match.group(1), "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            started = None
    stage = "proxy"
    for key, needle in _STAGES:
        if needle in segment:
            stage = key
    if f"=== mcp-proxy[{name}] CRASHED/STOPPED" in segment:
        stage = "crashed"
    return {"stage": stage, "size": size, "started": started}


def _newest_code_mtime() -> float:
    newest = 0.0
    for path in [REPO / "policy.toml", REPO / "fleet-projects.toml", *(REPO / "src").rglob("*.py")]:
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            pass
    return newest


def _proxy_state() -> dict:
    try:
        return json.loads(PROXY_STATE.read_text(encoding="utf-8")).get("instances", {})
    except (OSError, ValueError):
        return {}


def _port_listener_pids(port: int) -> set[int]:
    """PID владельцев LISTEN на локальном порту, а не совпадения по имени процесса.

    netstat — секунда без PowerShell; строка слушателя узнаётся по пустому удалённому
    адресу (0.0.0.0:0 / [::]:0), а не по локализованному слову LISTENING.
    """
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, timeout=20,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        text = out.decode("ascii", "replace") if isinstance(out, bytes) else str(out)
        found = set()
        for line in text.splitlines():
            parts = line.split()
            if (len(parts) >= 5 and parts[0].upper() == "TCP" and parts[1].endswith(f":{port}")
                    and parts[2] in ("0.0.0.0:0", "[::]:0") and parts[-1].isdecimal()):
                found.add(int(parts[-1]))
        found.discard(0)
        if found or "TCP" in text:
            return found
    except (subprocess.TimeoutExpired, OSError):
        pass
    output = _ps(
        f"Get-NetTCPConnection -State Listen -LocalPort {port} -ErrorAction SilentlyContinue | "
        "Select-Object -ExpandProperty OwningProcess -Unique"
    )
    return {int(part) for part in output.split() if part.isdecimal() and int(part) > 0}


def _process_command_line(pid: int) -> str:
    rows = _process_table()
    if rows is not None:
        row = next((r for r in rows if r["pid"] == pid), None)
        if row is not None:
            return row["cmd"]
        if _TABLE.get("full"):  # psutil видит все процессы: нет в снимке — процесса нет
            return ""
    return _ps(f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}' "
               "-ErrorAction SilentlyContinue).CommandLine")


def _protected_from_restart(command: str) -> bool:
    """Страж и его наблюдатель никогда не являются целью dev-restart."""
    normalized = command.replace("/", "\\").casefold()
    return any(marker in normalized for marker in (
        "run_guard.pyw", "guard-runtime", "guard_watch.py", "guard-observer",
    ))


def _proxy_command_for_port(command: str, port: int) -> bool:
    """Проверка stale PID из state.json до taskkill (PID мог быть переиспользован)."""
    return bool(re.search(r"\bmcp_proxy\b", command, re.IGNORECASE)
                and re.search(rf"(?:^|\s)--port\s+{port}(?=\s|$)", command))


def _proxy_victims(name: str, port: int) -> list[int]:
    """Только PID лончера либо TCP listener, с явным отказом трогать guard.

    state.json атомарен, но может отставать во время crash-loop, а PID может быть
    переиспользован. Для записи state без живого socket требуется подтверждённая
    командная строка mcp_proxy на ТОЧНО этом порту; любой портовый listener
    подтверждён самим ядром ОС. По подстроке всей таблицы процессов не ищем.
    """
    listeners = _port_listener_pids(port)
    state = _proxy_state().get(name, {})
    state_pid = state.get("pid") if isinstance(state, dict) else None
    pids = set(listeners)
    if type(state_pid) is int and state_pid > 0 and state.get("port") == port:
        pids.add(state_pid)

    selected = []
    for pid in sorted(pids):
        command = _process_command_line(pid)
        if not command:
            say("[!]", f"[{name}] PID {pid}: не удалось подтвердить процесс — не трогаю")
            continue
        if _protected_from_restart(command):
            say("[!]", f"[{name}] PID {pid}: страж/наблюдатель — не трогаю")
            continue
        if pid not in listeners and not _proxy_command_for_port(command, port):
            say("[!]", f"[{name}] PID {pid} из state устарел — не трогаю")
            continue
        selected.append(pid)
    return selected


def _stop_proxy_instance(name: str, port: int) -> list[int]:
    """Не используем /T: остановка dev не должна гасить соседние процессы."""
    victims = _proxy_victims(name, port)
    for pid in victims:
        result = subprocess.run(
            ["taskkill", "/F", "/PID", str(pid)],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        _invalidate_table()
        if result.returncode and _process_command_line(pid):
            fail(f"[{name}] taskkill PID {pid}: rc={result.returncode}")
    return victims


def step_restart_proxy(targets: list[str]) -> None:
    head(f"\n[2/5] Перезапуск mcp-proxy: {', '.join(targets)}")
    if not _ensure_launcher():
        return
    if targets == ["dev"]:
        say("[!]", "перезапуск dev рвёт текущее соединение с dev-mcp — если эту команду "
                   "запустил агент, его вызов оборвётся; итог будет в logs\\restart-last.txt")
    waiting: set[str] = set()
    for name in targets:
        port = INSTANCES[name]["port"]
        progress = _start_progress(name)
        if (not _port_open(port) and progress["stage"] in ("proxy", "child", "server")
                and progress["started"] and progress["started"] > _newest_code_mtime()
                and time.time() - progress["started"] < START_LIMIT_S):
            say("[..]", f"[{name}] уже поднимается с {time.strftime('%H:%M:%S', time.localtime(progress['started']))} "
                        f"(после последней правки кода; стадия: {_STAGE_TEXT[progress['stage']]}) — "
                        "не убиваю, дожидаюсь")
            waiting.add(name)
            continue
        victims = _stop_proxy_instance(name, port)
        if not victims and _port_open(port):
            fail(f"[{name}] порт {port} занят, но безопасный PID для остановки не найден")
            continue
        say("[..]", f"[{name}] убито процессов: {len(victims)} (лончер поднимет заново)")
    time.sleep(2)
    for name in targets:
        port = INSTANCES[name]["port"]
        # Лончер ждёт свои 10с backoff, плюс холодный старт: dev — секунды,
        # core — десяток секунд (obsidian ходит в npm за mcp-remote).
        started = time.monotonic()
        last_key, last_stage, changed_at, crashed_at = None, None, started, None
        while True:
            elapsed = time.monotonic() - started
            if _port_open(port):
                say("[OK]", f"[{name}] порт {port} поднялся ({elapsed:.0f}s) — {INSTANCES[name]['what']}")
                break
            progress = _start_progress(name)
            key = (progress["stage"], progress["size"])
            if key != last_key:
                last_key, changed_at = key, time.monotonic()
                if progress["stage"] and progress["stage"] != last_stage:
                    say("[..]", f"[{name}] {_STAGE_TEXT.get(progress['stage'], progress['stage'])} ({elapsed:.0f}s)")
                    last_stage = progress["stage"]
            crashed_at = (crashed_at or time.monotonic()) if progress["stage"] == "crashed" else None
            st = _proxy_state().get(name, {})
            log_name = st.get("log", f"mcp_proxy_{name}.log") if st else f"mcp_proxy_{name}.log"
            if crashed_at and time.monotonic() - crashed_at > 60:
                fail(f"[{name}] экземпляр упал и не перезапустился за минуту — смотри mcp-proxy\\{log_name}")
                break
            if time.monotonic() - changed_at > START_STALL_S:
                fail(f"[{name}] {START_STALL_S // 60} мин без движения в журнале на стадии "
                     f"«{_STAGE_TEXT.get(progress['stage'], progress['stage'] or '?')}» — смотри mcp-proxy\\{log_name}")
                break
            if elapsed > START_LIMIT_S:
                fail(f"[{name}] порт {port} не поднялся за {START_LIMIT_S // 60} мин — смотри mcp-proxy\\{log_name}")
                break
            time.sleep(1)


def step_local_hop(targets: list[str]) -> None:
    head("\n[3/5] Локальный хоп")
    for name in targets:
        inst = INSTANCES[name]
        url = f"http://127.0.0.1:{inst['port']}/servers/{inst['probe']}/mcp"
        ok, detail = _mcp_initialize(url)
        (say("[OK]", f"{url} — {detail}") if ok else fail(f"{url} — {detail}"))


def step_worker() -> None:
    head("\n[4/5] Воркер шлюза (poll_worker.py)")
    if _proc_alive("poll_worker.py"):
        say("[OK]", "воркер работает")
    else:
        # Раньше здесь звался C:\ops\orchestrator.ps1 — он поднимает элемент
        # по «порт мёртв», и во время крэш-лупа это добавляло ВТОРОЙ лончер mcp-proxy
        # поверх живого, то есть конфликт за порт. Поднимаем ровно воркер его же
        # обёрткой — тем же способом, что и 5-минутный сторож.
        say("[..]", f"воркер лежит — поднимаю {WORKER_VBS.name}")
        if not WORKER_VBS.exists():
            fail(f"обёртка воркера не найдена: {WORKER_VBS}")
            return
        subprocess.Popen(["wscript.exe", str(WORKER_VBS)], cwd=str(WORKER_VBS.parent))
        time.sleep(6)
        _invalidate_table()
        if _proc_alive("poll_worker.py"):
            say("[OK]", "воркер поднят")
        else:
            fail("воркер не поднялся — смотри worker\\worker.log")
            return
    # Три захода: один таймаут — не авария (23.09.2026: [X] при живом шлюзе, публичный хоп
    # следом прошёл). Настоящая проверка канала — шаг 5.
    errors = []
    for timeout in (10, 15, 20):
        try:
            with urllib.request.urlopen(GATEWAY_URL_FALLBACK + "/healthz", timeout=timeout) as r:
                h = json.loads(r.read().decode())
            say("[OK]" if h.get("queue_depth", 0) < 10 else "[!]",
                f"шлюз: queue_depth={h.get('queue_depth')}, inflight={h.get('inflight')}"
                + (f" (с {len(errors) + 1}-й попытки)" if errors else ""))
            return
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))
            time.sleep(2)
    fail(f"шлюз /healthz недоступен 3 раза подряд: {errors[-1]}")


def step_public_hop() -> None:
    head("\n[5/5] Публичный хоп через шлюз Oracle (то, что делает ChatGPT)")
    base = _env_value(WORKER_ENV, "WORKER_GATEWAY_URL") or GATEWAY_URL_FALLBACK
    token = _env_value(GATEWAY_ENV, "GATEWAY_CLIENT_TOKEN")
    if not token:
        say("[!]", f"токен не прочитан из {GATEWAY_ENV} — пропускаю (локально всё ок)")
        return
    url = f"{base}/mcp/dev?token={token}"
    ok, detail = _mcp_initialize(url, timeout=60)
    # 18.10: сразу после перезапуска dev воркер шлюза ещё держит соединение к старому
    # процессу и отдаёт 502 — через несколько секунд тот же запрос проходит.
    if not ok and (detail.startswith("URLError") or re.search(r"HTTP Error 50[234]", detail)):
        # До Oracle изредка не устанавливается само TCP-соединение (WinError 10060),
        # а через секунду всё отвечает. Ответа HTTP не было вообще — повторяем
        # один раз, иначе рестарт рисует [X] при живом маршруте.
        say("[!]", f"{detail[:90]}… — соединение не установилось, повторяю через 3с")
        time.sleep(8)
        ok, detail = _mcp_initialize(url, timeout=60)
    global _public_ok
    _public_ok = ok
    (say("[OK]", detail + "  — коннектор в ChatGPT будет работать") if ok
     else fail(detail + "  — коннектор в ChatGPT НЕ подключится"))


def step_deploy_receipt() -> None:
    """Квитанция `platform_deployed` после зелёного 5/5 для dev.

    С выключенным стражем (решение 0025) журнал platform-guard молчит, и блоки ревью
    с `reactivate_policy` platform_deploy_after ждали квитанцию, которую до 18.10 писали
    только вручную (последняя — 16.10). Квитанция — только на чистый `src/`: иначе
    работающий код не равен коммиту, который она называет.
    """
    head("\nКвитанция деплоя")
    git = lambda *a: subprocess.run(["git", *a], cwd=REPO, capture_output=True, text=True,  # noqa: E731
                                    encoding="utf-8", errors="replace", timeout=30)
    sha = git("rev-parse", "HEAD").stdout.strip()
    dirty = git("status", "--porcelain", "--", "src").stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or dirty:
        say("[!]", f"не записана: {'src/ не закоммичен' if dirty else 'HEAD не прочитан'}")
        return
    try:
        from dev_mcp.fleet.location import fleet_db_path
        from dev_mcp.fleet.store import Store

        store = Store.open(str(fleet_db_path(REPO)))
        with store.tx() as tx:
            last = tx.one("SELECT kind,payload FROM events WHERE kind IN "
                          "('platform_deployed','platform_rolled_back') ORDER BY id DESC LIMIT 1")
            if last and last["kind"] == "platform_deployed" and \
                    json.loads(last["payload"] or "{}").get("candidate") == sha:
                say("[OK]", f"{sha[:10]} уже квитирован")
                return
            ts = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
            tx.record_event(kind="platform_deployed", payload={
                "candidate": sha, "known_good": sha, "ts": ts,
                "producer": "restart.py", "scope": "dev only",
                "evidence": {"restart": "5/5", "public_initialize": True,
                             "dev_listeners": sorted(_port_listener_pids(8766))},
            })
        say("[OK]", f"platform_deployed {sha[:10]} — блоки «до деплоя платформы» оттают")
    except Exception as exc:  # noqa: BLE001 — рестарт уже зелёный, квитанция вторична
        say("[!]", f"не записана: {type(exc).__name__}: {exc}"[:200])


def main() -> int:
    parser = argparse.ArgumentParser(description="Check and restart selected MCP proxy instances.")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--all", action="store_true", help="restart dev and core")
    scope.add_argument("--core", action="store_true", help="restart core only")
    scope.add_argument("--instance", choices=sorted(INSTANCES), action="append",
                       help="restart only these instances (aux, lib: без рестарта dev/core)")
    parser.add_argument("--local-only", action="store_true", help="skip public gateway checks")
    parser.add_argument("--no-tests", action="store_true", help="skip the production test selection")
    parser.add_argument("--force-tests", action="store_true",
                        help="run the production guards even if nothing changed since the last green run")
    args = parser.parse_args()
    local_only = args.local_only
    run_tests = not args.no_tests
    if args.instance:
        targets = list(dict.fromkeys(args.instance))
    elif args.all:
        targets = ["dev", "core"]
    elif args.core:
        targets = ["core"]
    else:
        targets = ["dev"]

    head("=" * 60)
    head(f" restart dev-mcp  ({'+'.join(targets)})")
    head("=" * 60)
    free = _free_ram_gib()
    if free is not None and free < 1.0:
        say("[!]", f"свободно {free:.2f} ГиБ RAM — запуск dev может занять 10+ мин (всё упирается в подкачку). "
                   "Закрой тяжёлые процессы (сборки, лишние вкладки); restart.cmd повторно не запускай — "
                   "он дождётся сам.")

    if step_check_policy(run_tests, args.force_tests) is None:
        head("\n" + "=" * 60)
        head(" СТОП: policy.toml не в порядке — сервис НЕ перезапущен.")
        head("=" * 60)
        write_report()
        return 1

    step_restart_proxy(targets)
    step_local_hop(targets)
    if not local_only:
        step_worker()
        if "dev" in targets:
            step_public_hop()
            if _ok and _public_ok:
                step_deploy_receipt()

    head("\n" + "=" * 60)
    head(" ГОТОВО — всё зелёное." if _ok else " ЗАВЕРШЕНО С ОШИБКАМИ — см. [X] выше.")
    head("=" * 60)
    write_report()
    return 0 if _ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
