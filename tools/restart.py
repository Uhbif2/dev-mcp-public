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
        --local-only   только шаги 1–3 (не трогать воркер и шлюз)
        --no-tests     пропустить боевые стражи (шаг 1 оставляет только синтаксис)

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

# Экземпляры mcp-proxy. Должно совпадать с INSTANCES в mcp_launcher.py.
INSTANCES = {
    "dev": {"port": 8766, "probe": "dev", "what": "dev-mcp (флот агентов)"},
    "core": {"port": 8765, "probe": "memory_service", "what": "memory_service, obsidian, google-sheets, demo-service"},
}

INIT_BODY = json.dumps({
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "restart.py", "version": "0"}},
}).encode()

_ok = True
_public_ok = False
_lines: list[str] = []


def say(mark: str, msg: str) -> None:
    _lines.append(f"  {mark} {msg}")
    print(f"  {mark} {msg}", flush=True)


def head(msg: str) -> None:
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
    r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                       capture_output=True, text=True, timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return (r.stdout or "").strip()


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
    decls = "; ".join(f"$n{i} = {_split_literal(n)}" for i, n in enumerate(needles))
    cond = " -or ".join(f"$_.CommandLine -like \"*$n{i}*\"" for i in range(len(needles)))
    out = _ps(f"{decls}; Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' OR Name='python.exe'\" | "
              f"Where-Object {{ {cond} }} | ForEach-Object {{ $_.ProcessId }}")
    return [int(x) for x in out.split() if x.strip().isdigit()]


def _proc_alive(needle: str) -> bool:
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

def step_check_policy(run_tests: bool) -> Policy | None:
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
    if run_tests:
        r = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q", "-k", "production",
                            "--no-header", "-p", "no:cacheprovider"],
                           cwd=str(REPO), capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        last = (r.stdout or r.stderr).strip().splitlines()[-1] if (r.stdout or r.stderr).strip() else ""
        if r.returncode == 0:
            say("[OK]", f"боевые стражи: {last}")
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
    if _pids_of("mcp_launcher.py"):
        return True
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


def _proxy_state() -> dict:
    try:
        return json.loads(PROXY_STATE.read_text(encoding="utf-8")).get("instances", {})
    except (OSError, ValueError):
        return {}


def _port_listener_pids(port: int) -> set[int]:
    """PID владельцев LISTEN на локальном порту, а не совпадения по имени процесса."""
    output = _ps(
        f"Get-NetTCPConnection -State Listen -LocalPort {port} -ErrorAction SilentlyContinue | "
        "Select-Object -ExpandProperty OwningProcess -Unique"
    )
    return {int(part) for part in output.split() if part.isdecimal() and int(part) > 0}


def _process_command_line(pid: int) -> str:
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
    for name in targets:
        port = INSTANCES[name]["port"]
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
        for i in range(1, 121):
            if _port_open(port):
                say("[OK]", f"[{name}] порт {port} поднялся ({i}s) — {INSTANCES[name]['what']}")
                break
            time.sleep(1)
        else:
            st = _proxy_state().get(name, {})
            hint = f", состояние по state.json: {st.get('state')}" if st else ""
            fail(f"[{name}] порт {port} не поднялся за 2 мин{hint} — смотри "
                 f"mcp-proxy\\{st.get('log', 'mcp_proxy.log')}")


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
    parser.add_argument("--local-only", action="store_true", help="skip public gateway checks")
    parser.add_argument("--no-tests", action="store_true", help="skip the production test selection")
    args = parser.parse_args()
    local_only = args.local_only
    run_tests = not args.no_tests
    if args.all:
        targets = ["dev", "core"]
    elif args.core:
        targets = ["core"]
    else:
        targets = ["dev"]

    head("=" * 60)
    head(f" restart dev-mcp  ({'+'.join(targets)})")
    head("=" * 60)

    if step_check_policy(run_tests) is None:
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
