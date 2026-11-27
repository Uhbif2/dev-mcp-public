"""Быстрые агенты — субагенты владельца в чатах ChatGPT (решение владельца 15.11.2026).

Зачем. Очередь флота с ТЗ, приёмкой и независимым ревью нужна для работы, которую никто не
ждёт. Когда Claude Code или ведущий хочет отдать кусок работы и сразу получить ответ, эта
бюрократия только мешает. Быстрый агент — чат ChatGPT, подчинённый одному владельцу:

* без очереди, ТЗ, захвата, ревью и интеграции — задача уходит в новый чат текстом;
* ответ исполнителя (последний законченный ход) сразу попадает владельцу: Claude Code —
  `fleet.cmd quick wait/show`, ведущему — сообщением в конверт и `fleet_quick show`;
* полная подконтрольность: владелец дописывает в чат (`say`), закрывает (`close`), видит
  ход (`show`: последний ответ, действия агента из журнала dev-mcp, время тишины);
* права наследуются от владельца: у быстрого агента ведущего — захваты ведущего (страж
  записи считает их своими), у агента владельца с ролью owner — роль owner (пульт, браузер);
  область записи — корень проекта задачи (или явный `scope`);
* у быстрого агента Claude Code явный `scope` — область ОПЕРАТОРА (владелец, 15.11): запись и
  run там разрешены, даже где агентам закрыто (tree_deny: dev-mcp), — на время каждого его
  вызова и пока задание открыто (`operator_roots`, policy.DELEGATED_ROOTS). Коммит, push и
  перезапуск остаются за Claude Code.

Чат открывает и ведёт расширение `fleet-tabs` (метка `pult-<id>`): `fleet_quick_orders` →
открыть/отправить, `fleet_quick_report` ← адрес чата и законченный ход. Агент передаёт
`agent="Q<n>[-OWNER]"` во всех вызовах dev-mcp — так его записи и запуски видны в журнале.
"""
from __future__ import annotations

import json
import math
import os
import re
import statistics
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

MARK = "⟦быстрый агент {tag}⟧"
OPEN_STATES = ("new", "working", "answered", "parked", "answered-unparked")
PARK_HOLD_S = 15.0
PARK_LIMIT_S = 6 * 60.0
PARK_POLL_S = 0.5
PARK_MAX_CONCURRENT = 4
CLOSE_RETRY_S = 24 * 60 * 60  # repeat close orders after MV3 loses tab/label bindings
_park_slots = threading.BoundedSemaphore(PARK_MAX_CONCURRENT)
# Different per server process. Persisting it distinguishes a lost in-flight
# park call on restart from a normal six-minute parking limit.
_PARK_EPOCH = uuid.uuid4().hex
WAKE_SILENCE_S = 4 * 60.0
WAKE_MAX = 2
CLAIM_NOTE_LIMIT = 3500
_ID = re.compile(r"^[qQ](\d+)(?:-([a-zA-Z0-9]+))?(?:\.([2-9]\d*))?$")
_local = threading.Lock()


def _pid_alive(pid: int) -> bool:
    """Жив ли процесс. На Windows без os.kill(pid, 0) — он там завершает процесс."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: процесс есть, чужой
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lock_is_stale(lockfile: Path, limit_s: float) -> bool:
    """Замок можно забрать: владелец-процесс мёртв или замок старше limit_s (зависший писатель)."""
    try:
        age = time.time() - lockfile.stat().st_mtime
        raw = lockfile.read_text(encoding="ascii", errors="ignore").strip()
    except OSError:
        return False
    if age > limit_s:
        return True
    if raw.isdigit():
        return age > 1 and not _pid_alive(int(raw))
    return age > 15  # старый формат без pid — прежнее правило
_cache: dict[str, Any] = {"path": None, "mtime": None, "jobs": {}}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def norm_id(value: str) -> str:
    """Normalize legacy q12 and owner-qualified q12-7f2a91 IDs."""
    text = str(value or "").strip()
    if text.isdigit():
        return f"q{text}"
    m = _ID.match(text)
    if not m:
        raise ValueError(f"номер быстрого агента вида q12 или q12-7f2a91, а не {value!r}")
    return (f"q{m.group(1)}" + (f"-{m.group(2).lower()}" if m.group(2) else "")
            + (f".{m.group(3)}" if m.group(3) else ""))


def claude_owner(session_id: str | None) -> str:
    """Legacy Claude without a session ID stays `claude`; new sessions are isolated."""
    prefix = str(session_id or "").strip()[:6]
    return f"claude:{prefix}" if prefix else "claude"


def _owner_suffix(owner: str) -> str:
    if owner == "claude":
        return ""  # pre-session jobs keep their original qN IDs
    suffix = owner.partition(":")[2] if owner.startswith("claude:") else owner
    suffix = suffix.lower()
    if re.fullmatch(r"[a-z0-9]+", suffix):
        return suffix
    # Nonstandard session/worker IDs cannot introduce punctuation into a worker tag.
    import hashlib
    return hashlib.sha256(owner.encode("utf-8")).hexdigest()[:8]


def tag_of(job_id: str) -> str:
    return norm_id(job_id).upper()  # Q12 — agent в вызовах dev-mcp и worker_id в реестре


def is_quick_agent(agent: str) -> bool:
    text = str(agent or "").strip()
    return text.startswith("Q") and bool(_ID.fullmatch(text))


class Book:
    def __init__(self, data_dir: Path) -> None:
        self.dir = Path(data_dir) / "quick"
        self.path = self.dir / "jobs.json"
        self.lockfile = self.dir / "jobs.lock"

    @contextmanager
    def locked(self) -> Iterator[dict[str, Any]]:
        """Сервер и пульт пишут одну книгу: межпроцессный замок файлом + атомарная запись."""
        self.dir.mkdir(parents=True, exist_ok=True)
        with _local:
            deadline = time.monotonic() + 30
            while True:
                try:
                    fd = os.open(self.lockfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.write(fd, str(os.getpid()).encode())
                    os.close(fd)
                    break
                except FileExistsError:
                    # 18.11: замок старше 15 с крали у ЖИВОГО писателя (запись 5,5 МБ на нагруженной
                    # машине дольше), оба писали в общий jobs.tmp — книга склеилась из двух версий
                    # («Extra data»), и все быстрые агенты пропали из списка. Теперь крадём только у
                    # мёртвого процесса или по явному пределу 120 с.
                    if _lock_is_stale(self.lockfile, 120):
                        try:
                            self.lockfile.unlink()
                        except OSError:
                            pass
                        continue
                    if time.monotonic() > deadline:
                        raise TimeoutError("книга быстрых агентов занята")
                    time.sleep(0.05)
            try:
                jobs = self.jobs(strict=True)
                yield jobs
                # Своё имя временного файла у каждого писателя: общий jobs.tmp при гонке смешивал потоки.
                tmp = self.path.with_name(f"jobs.{os.getpid()}.{threading.get_ident()}.tmp")
                tmp.write_text(json.dumps(jobs, ensure_ascii=False, indent=1), encoding="utf-8")
                for attempt in range(40):  # Windows: читатель держит файл → PermissionError на replace
                    try:
                        os.replace(tmp, self.path)
                        break
                    except PermissionError:
                        if attempt == 39:
                            raise
                        time.sleep(0.05)
            finally:
                try:
                    self.lockfile.unlink()
                except OSError:
                    pass

    def jobs(self, *, strict: bool = False) -> dict[str, Any]:
        """Книга. Нет файла — пусто; занят/битый — повтор (15.11: `quick wait` видел «агента нет»
        в момент os.replace). strict (под замком перед записью) — ошибка вместо пустой книги,
        иначе запись затёрла бы все задания."""
        if not self.path.exists():
            return {}
        last: Exception | None = None
        for _ in range(40):
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            except FileNotFoundError:
                return {}
            except (OSError, ValueError) as exc:
                last = exc
                time.sleep(0.05)
        if strict:
            raise RuntimeError(f"книга быстрых агентов не читается: {last}")
        return {}


def lookup(book: Book, agent: str) -> dict[str, Any] | None:
    """Задача по полному agent (Q12 / Q1-7F2A91); кеш для частого вызова стража."""
    if not is_quick_agent(agent):
        return None
    try:
        mtime = book.path.stat().st_mtime_ns
    except OSError:
        return None
    if _cache["path"] != str(book.path) or _cache["mtime"] != mtime:
        _cache.update(path=str(book.path), mtime=mtime, jobs=book.jobs())
    return next((j for j in _cache["jobs"].values()
                 if str(j.get("tag") or "").upper() == str(agent).upper()), None)


def first_message(job: dict[str, Any]) -> str:
    tag = job["tag"]
    owner = ("Claude Code владельца" if job["owner"] == "claude" or
             job["owner"].startswith("claude:") else f"агента {job['owner']}")
    scope = job.get("scope") or []
    where = (f"Проект: {job['project']} — {job['root']}." if job.get("root") else
             "Проект не задан: работай там, где укажет задача.")
    rights = ("Права — как у владельца: " + (", ".join(scope) if scope else "весь корень проекта")
              + "; запись вне них страж dev-mcp не пропустит.")
    if operator_roots(job):
        rights += (" Это права оператора Claude Code: write_file/edit_file/run разрешены в этой области, "
                   "даже если она закрыта флоту (dev-mcp). НЕ коммить, НЕ пушь и НЕ перезапускай "
                   "dev-mcp (restart) — это делает Claude Code после проверки. Тесты гоняй только "
                   "затронутые.")
    from . import memory_rules
    memory = memory_rules.render(str(job.get("project") or ""), str(job["prompt"]),
                                 "quick", quick_owner=str(job.get("owner") or ""))
    return "\n".join([
        MARK.format(tag=tag),
        f"Ты — быстрый исполнитель {owner}. Очереди, ТЗ, захвата и ревью нет: твой ответ в этом "
        "чате НЕ является каналом итога: результат передаётся владельцу через MCP.",
        f"Во ВСЕХ вызовах инструментов dev-mcp передавай agent=\"{tag}\". fleet_join и fleet_take не "
        "вызывай — ты не воркер очереди.",
        where, rights,
        "Делай сразу. Итог передай через fleet_quick(agent=твой tag, command='report', "
        "args=[полный результат: сделано, файлы, проверки, остаток]). Результат попадёт владельцу "
        "через сервер, не через DOM чата. Затем НЕ заканчивай ход: report паркует до 15 секунд. "
        "Если ответ 'ожидай' — вызывай fleet_quick(agent=tag, command='park') снова. "
        "Полученное новое поручение исполни, снова сдай report. Если 'закрыт' или 'лимит' — "
        "заверши ход одной строкой. В чат до завершения пиши лишь кратко.",
        "", *(memory.splitlines() + [""] if memory else []), "Задача:", job["prompt"],
    ])


def create(book: Book, *, owner: str, prompt: str, project: str = "", root: str = "",
           scope: list[str] | None = None, register: Callable[[str, str], None] | None = None,
           home: str = "", after: list[str] | None = None) -> dict[str, Any]:
    prompt = str(prompt or "").strip()
    if not prompt:
        raise ValueError("нужен текст задачи")
    if len(prompt) > 30_000:
        raise ValueError("задача длиннее 30 000 знаков — положи её в файл проекта и сошлись на него")
    with book.locked() as jobs:
        dependencies = list(dict.fromkeys(_resolve(jobs, item, owner)["id"] for item in (after or [])))
        n = 1 + max((int(m.group(1)) for j in jobs.values()
                     if j.get("owner") == owner and (m := _ID.fullmatch(str(j.get("id", ""))))),
                    default=0)
        suffix = _owner_suffix(str(owner))
        jid = f"q{n}-{suffix}" if suffix else f"q{n}"
        while jid in jobs:  # pre-existing legacy names or truncated-session collision
            n += 1
            jid = f"q{n}-{suffix}" if suffix else f"q{n}"
        job = {"id": jid, "tag": jid.upper(), "owner": str(owner), "project": project, "root": root,
               "scope": list(scope or []), "prompt": prompt,
               "state": "waiting" if dependencies else "new", "after": dependencies, "created": _now(),
               "updated": _now(), "url": "", "home": home, "outbox": [], "messages": [],
               "answers": 0, "last_answer": "", "answered_at": "",
               "delivered": 0,  # owner wait cursor; not delivery to the agent's chat
               "new_at": "" if dependencies else _now(), "chat_opened_at": "",
               "first_delivered_at": "", "first_action_at": "", "reported_at": "",
               "notified_at": "", "outbox_since_at": "" if dependencies else _now(),
               "park_started": "", "park_wanted": False, "park_epoch": "",
               "inbox": [], "wake_count": 0}
        # Validate transitive dependencies as well; corrupted/imported books must not create cycles.
        def visit(node: str, seen: set[str]) -> None:
            if node == jid or node in seen:
                raise ValueError(f"цикл зависимостей быстрого агента: {node}")
            for parent in jobs.get(node, {}).get("after") or []:
                if parent == jid:
                    raise ValueError(f"цикл зависимостей быстрого агента: {parent}")
                if parent not in jobs:
                    raise ValueError(f"несуществующий id зависимости: {parent}")
                visit(parent, seen | {node})
        for dependency in dependencies:
            visit(dependency, set())
        job["outbox"].append(first_message(job))
        jobs[jid] = job
    if register is not None:
        register(job["tag"], f"pult-{jid}")
    return job


def adopt(book: Book, job_id: str, *, owner: str, idle_minutes: int = 30,
          force: bool = False) -> dict[str, Any]:
    """Transfer an existing Claude quick job to another Claude session, with audit trail."""
    jid = norm_id(job_id)
    if "-" not in jid or not owner.startswith("claude:"):
        raise PermissionError("quick adopt требует полный ID и сессию оператора Claude Code")
    with book.locked() as jobs:
        job = jobs.get(jid)
        if job is None:
            raise ValueError(f"быстрого агента {jid} нет")
        previous = str(job.get("owner", ""))
        if not previous.startswith("claude:"):
            raise PermissionError("quick adopt разрешён только для заданий claude:*")
        if previous == owner:
            return dict(job)
        # report() refreshes updated on extension polls; that is not owner activity.
        owner_messages = [str(m.get("ts") or "") for m in job.get("messages", [])
                          if m.get("role") == "owner"]
        last = max([str(job.get("created") or ""),
                    str(job.get("owner_activity_at") or ""), *owner_messages])
        try:
            timestamp = datetime.fromisoformat(last)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            idle = (datetime.now(timezone.utc) - timestamp).total_seconds() / 60
        except ValueError:
            idle = 0
        if not force and idle < idle_minutes:
            raise PermissionError(f"{jid}: прежняя сессия активна ({idle:.1f} мин тишины); нужен --force или {idle_minutes} мин")
        now = _now()
        job["owner"] = owner
        job["updated"] = now
        job.setdefault("events", []).append({"kind": "adopt", "from": previous,
                                              "to": owner, "at": now, "force": bool(force)})
        return dict(job)


def owned(job: dict[str, Any] | None, owner: str) -> dict[str, Any]:
    if job is None:
        raise ValueError("такого быстрого агента нет")
    if owner != "*" and job["owner"] != owner and not (
        owner.startswith("claude:") and job["owner"] == "claude"
    ):
        raise PermissionError(f"{job['id']} принадлежит {job['owner']}, не {owner}")
    return job


def _resolve(jobs: dict[str, Any], job_id: str, owner: str) -> dict[str, Any]:
    """A short qN is valid only when it identifies one accessible job.

    Old shared Claude jobs (qN) can overlap with session-local jobs (qN-xxxxxx).
    Never pick one by preference: in particular, close must not close the wrong
    session's job. qN-legacy explicitly addresses a shared old qN.
    """
    jid = norm_id(job_id)
    if jid.endswith("-legacy"):
        return owned(jobs.get(jid[:-7]), owner)  # qN-legacy disambiguates a shadowed old qN
    if "-" in jid:
        return owned(jobs.get(jid), owner)
    if owner != "*":
        candidates = []
        for job in jobs.values():
            if job.get("owner") == owner and _ID.fullmatch(str(job.get("id", ""))):
                if _ID.fullmatch(job["id"]).group(1) == jid[1:]:
                    candidates.append(job)
        legacy = jobs.get(jid)
        if legacy and legacy["id"] not in {j["id"] for j in candidates}:
            try:
                owned(legacy, owner)
            except PermissionError:
                pass
            else:
                candidates.append(legacy)
        if len(candidates) > 1:
            choices = [f"{jid}-legacy (старый общий {jid})" if j["id"] == jid
                       else j["id"] for j in candidates]
            raise ValueError(
                f"короткий ID {jid} неоднозначен: {', '.join(choices)}. "
                "Укажи один из полных ID; команда не выполнена")
        if candidates:
            return owned(candidates[0], owner)
    return owned(jobs.get(jid), owner)


def say(book: Book, job_id: str, text: str, *, owner: str) -> dict[str, Any]:
    text = str(text or "").strip()
    if not text:
        raise ValueError("пустое сообщение")
    with book.locked() as jobs:
        job = _resolve(jobs, job_id, owner)
        if job["state"] == "closed":
            raise ValueError(f"{job['id']} закрыт")
        if job["state"] == "waiting":
            raise ValueError(f"{job['id']} ждёт завершения зависимостей")
        if (job.get("state") == "parked" or
                (job.get("state") == "answered-unparked" and job.get("park_wanted"))):
            # An in-flight park lost at restart remains recoverable; do not
            # reopen the browser merely because its old MCP request died.
            job.setdefault("inbox", []).append(text)
        else:
            if not job.get("outbox"):
                job["outbox_since_at"] = _now()
            job["outbox"].append(text)
            # Enqueueing is not a browser send or proof of an acting agent.
            # In particular preserve answered-unparked for wait until sent.
            job["wake_count"] = 0
        job["owner_activity_at"] = _now()
        job["updated"] = _now()
        return dict(job)


def close(book: Book, job_id: str, *, owner: str) -> dict[str, Any]:
    with book.locked() as jobs:
        job = _resolve(jobs, job_id, owner)
        job["state"] = "closed"
        job["closed_at"] = job.get("closed_at") or _now()
        job["outbox"] = []
        job["inbox"] = []
        job["park_wanted"] = False
        job["updated"] = _now()
        return dict(job)


def prepare_successor(book: Book, agent: str, old_url: str, context: str,
                      token: str) -> dict[str, Any]:
    """Rotate ChatGPT conversation and Q tag, preserving the logical owner job.

    Browser closes the old *owned* tab and starts a fresh ChatGPT conversation.
    A repeated handoff request is idempotent, even across server restarts.
    """
    jid = norm_id(agent).split(".")[0]
    with book.locked() as jobs:
        job = jobs.get(jid)
        if not job or (job["tag"] != str(agent).upper() and
                       (job.get("chat_handoff") or {}).get("old_tag") != str(agent).upper()) or job["state"] == "closed":
            raise PermissionError("handoff: quick agent not active")
        previous = job.get("chat_handoff") or {}
        if previous.get("token") == token:
            return dict(job)
        if previous.get("status") == "pending":
            raise PermissionError("handoff already has a pending successor")
        if not job.get("url") or job["url"].rstrip("/") != old_url.rstrip("/"):
            raise PermissionError("handoff: source conversation is not registered to agent")
        from .chat_handoff import rotated_tag
        new_tag = rotated_tag(str(job["tag"]))
        job["tag"] = new_tag
        startup = first_message(job) + "\n\n" + context
        job["chat_handoff"] = {"token": token, "old_url": old_url,
                               "old_tag": str(agent).upper(), "new_tag": new_tag,
                               "status": "pending", "at": _now()}
        job["url"] = ""
        job["outbox"].insert(0, startup)
        job["outbox_since_at"] = _now()
        job["state"] = "new"
        job["generating"] = False
        job["park_wanted"] = False
        job["park_started"] = ""
        job["wake_pending"] = False
        job["updated"] = _now()
        return dict(job)


def get(book: Book, job_id: str, *, owner: str = "*") -> dict[str, Any]:
    return dict(_resolve(book.jobs(), job_id, owner))


def listing(book: Book, *, owner: str = "*", include_closed: bool = False) -> list[dict[str, Any]]:
    rows = [j for j in book.jobs().values() if (owner == "*" or j["owner"] == owner)
            and (include_closed or j["state"] != "closed")]
    return sorted(rows, key=lambda j: (int(_ID.fullmatch(j["id"]).group(1)), j["id"]))


def collect_wait(book: Book, job_ids: list[str], *, owner: str, after: int | None = None,
                 all_answers: bool = False) -> list[dict[str, Any]]:
    """Take one unread finished reply (or all), acknowledging only returned jobs.

    The read-only fast path avoids rewriting the shared book on each wait poll.
    The locked recheck prevents duplicate delivery when two waiters race.
    Explicit --after overrides the saved cursor; otherwise old replies are
    available once, even when they arrived before the wait command started.
    """
    def select(jobs: dict[str, Any]) -> list[dict[str, Any]]:
        # IDs have already been resolved by the CLI. Do not resolve short qN
        # again: an explicit qN-legacy may be shadowed by this session's qN.
        matches = [owned(jobs.get(jid), owner) for jid in job_ids]
        # MCP report has already persisted a finished answer. A queued say
        # must not hide it; only unfinished browser/DOM turns wait for outbox.
        # Even in answered-unparked/closed, an MCP answer remains durable for
        # show, but must only wake an implicit wait until the owner reads it.
        ready = [j for j in matches if (
            int(j.get("answers") or 0) >
            (after if after is not None else int(j.get("delivered") or 0))
        ) and (not j.get("outbox") or j.get("answer_source") == "mcp")]
        if all_answers:
            return ready if len(ready) == len(matches) else []
        return sorted(ready, key=lambda j: (j.get("answered_at") or "", job_ids.index(j["id"])))[:1]

    if not select(book.jobs()):
        return []
    with book.locked() as jobs:
        picked = select(jobs)
        for job in picked:
            job["delivered"] = max(int(job.get("delivered") or 0), job["answers"])
            job["notified_at"] = job.get("notified_at") or _now()
        return [dict(j) for j in picked]


def exchange(book: Book, agent: str, answer: str = "", *,
             deliver: Callable[[dict[str, Any], str], None] | None = None,
             hold_s: float = PARK_HOLD_S, limit_s: float = PARK_LIMIT_S,
             poll_s: float = PARK_POLL_S) -> dict[str, Any]:
    """MCP report/park: durable answer, then bounded wait without a browser tab.

    Each wait holds one gateway lane; callers must bound simultaneous parking.
    File lock is never held across the wait. A nonempty answer is saved before polling.
    """
    jid = norm_id(agent).split(".")[0]
    # MCP audit is written after each tool call. At report time all preceding
    # non-report actions are durable; capture their first timestamp in the job.
    prior = book.jobs().get(jid)
    action_at = (first_actions(book.dir.parents[2] / "logs", [prior])
                 .get(str(agent).upper(), "") if prior and answer.strip()
                 and answer.strip() != prior.get("last_answer")
                 and not prior.get("first_action_at") else "")
    fresh = None
    with book.locked() as jobs:
        job = jobs.get(jid)
        if not job or job.get("tag") != str(agent).upper():
            raise PermissionError("quick report: неизвестный agent")
        if job["state"] == "closed":
            return {"status": "closed", "message": "Закрыт: заверши ход одной строкой"}
        now = _now()
        if answer.strip():
            answer = answer.strip()[-60_000:]
            if answer != job.get("last_answer"):
                job["last_answer"] = answer
                job["answers"] = int(job.get("answers") or 0) + 1
                job["answered_at"] = now
                job["reported_at"] = job.get("reported_at") or now
                job["first_action_at"] = job.get("first_action_at") or action_at
                job["answer_source"] = "mcp"
                job["wake_count"] = 0
                job.setdefault("messages", []).append({"role": "agent", "text": answer, "ts": now})
                job["messages"] = job["messages"][-40:]
                fresh = dict(job)
            job["park_started"] = now
            job["park_epoch"] = _PARK_EPOCH
            job["park_wanted"] = True
        elif not job.get("park_started"):
            raise ValueError("сначала сдай непустой итог через command='report'")
        elif job.get("park_wanted") and job.get("park_epoch") != _PARK_EPOCH:
            # The previous MCP request was cut off by a server restart.
            # Resume the parked turn with a fresh bounded lease.
            job["park_started"] = now
            job["park_epoch"] = _PARK_EPOCH
        job["state"] = "parked"
        job["updated"] = now
    if fresh is not None and deliver is not None:
        try:
            deliver(fresh, fresh["last_answer"])
            with book.locked() as jobs:
                jobs[jid]["notified_at"] = jobs[jid].get("notified_at") or _now()
        except Exception:
            pass  # Result is durable in book; owner can collect via wait/show.

    if not _park_slots.acquire(blocking=False):
        with book.locked() as jobs:
            if jobs[jid]["state"] != "closed":
                jobs[jid]["state"] = "answered-unparked"
                jobs[jid]["park_started"] = ""
                jobs[jid]["park_wanted"] = False
        return {"status": "limit", "message": "Лимит одновременной парковки: заверши ход одной строкой"}
    try:
        deadline = time.monotonic() + max(0.0, min(hold_s, 30.0))
        while True:
            # Read-only polling: don't rewrite the shared JSON book each 500 ms.
            snapshot = book.jobs().get(jid, {})
            if snapshot.get("state") == "closed":
                return {"status": "closed", "message": "Закрыт: заверши ход одной строкой"}
            started = _parse_time(snapshot.get("park_started"))
            expired = bool(started and (datetime.now(timezone.utc) - started).total_seconds() >= limit_s)
            if snapshot.get("inbox") or expired:
                with book.locked() as jobs:
                    job = jobs[jid]
                    if job["state"] == "closed":
                        return {"status": "closed", "message": "Закрыт: заверши ход одной строкой"}
                    if job.get("inbox"):
                        instruction = job["inbox"].pop(0)
                        job["state"] = "working"
                        job["park_started"] = ""
                        job["park_wanted"] = False
                        job["updated"] = _now()
                        return {"status": "task", "message": instruction}
                    if expired:
                        job["state"] = "answered-unparked"
                        job["park_started"] = ""
                        job["park_wanted"] = False
                        job["updated"] = _now()
                        return {"status": "limit", "message": "Лимит парковки: заверши ход одной строкой"}
            if time.monotonic() >= deadline:
                return {"status": "parked", "message": "Ожидай: СРАЗУ повтори fleet_quick(agent=tag, command='park')"}
            time.sleep(min(poll_s, max(0.01, deadline - time.monotonic())))
    finally:
        _park_slots.release()


def _parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def first_actions(logs_dir: Path, jobs: Any) -> dict[str, str]:
    """First non-fleet_quick tool per task, from durable MCP audit.

    The middleware only passes (book, job) to mark_acting, not the tool
    name. Never mistake fleet_quick(report/park) for substantive work.
    The audit's timestamp is the closest available proof of that action.
    """
    targets = {str(j.get("tag") or "").upper(): _parse_time(j.get("new_at") or j.get("created"))
               for j in jobs}
    first: dict[str, str] = {}
    if not targets:
        return first
    logs_dir = Path(logs_dir)
    files = sorted(logs_dir.glob("audit-*.jsonl"))[-2:] + [logs_dir / "audit.jsonl"]
    for file in files:
        try:
            with file.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        entry = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    tag = str(entry.get("agent") or "").upper()
                    if (tag not in targets or
                            str(entry.get("tool") or "").startswith("fleet_quick")):
                        continue
                    stamp = _parse_time(entry.get("ts"))
                    if stamp is None or (targets[tag] and stamp < targets[tag]):
                        continue
                    timestamp = stamp.isoformat()
                    if tag not in first or timestamp < first[tag]:
                        first[tag] = timestamp
        except OSError:
            continue
    return first


def with_action(job: dict[str, Any], actions: dict[str, str]) -> dict[str, Any]:
    """Enrich a stored job for presentation without rewriting the shared book."""
    result = dict(job)
    result["first_action_at"] = result.get("first_action_at") or actions.get(str(job.get("tag") or ""), "")
    # Old records have sent owner messages but no explicit delivery milestone.
    if not result.get("first_delivered_at"):
        sent = [str(m.get("ts") or "") for m in job.get("messages") or []
                if m.get("role") == "owner" and _parse_time(m.get("ts"))]
        result["first_delivered_at"] = min(sent) if sent else ""
    return result


def format_delay(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "—"
    minutes, remainder = divmod(int(seconds), 60)
    return f"{minutes}м{remainder:02d}с"


def recent_summary(book: Book, actions: dict[str, str], *,
                   now: datetime | None = None) -> str:
    """Observed (not guessed) new→first substantive action latency over 24 h."""
    now = now or datetime.now(timezone.utc)
    recent = []
    for job in book.jobs().values():
        created = _parse_time(job.get("new_at") or job.get("created"))
        if created is not None and 0 <= (now - created).total_seconds() <= 86400:
            recent.append(job)
    delays = []
    for job in recent:
        started = _parse_time(job.get("new_at") or job.get("created"))
        action = _parse_time(job.get("first_action_at") or actions.get(str(job.get("tag") or "")))
        if started and action and action >= started:
            delays.append((action - started).total_seconds())
    if not delays:
        return f"quick 24 ч: заданий {len(recent)}; первое действие не измерено"
    delays.sort()
    return (f"quick 24 ч: заданий {len(recent)}, с первым действием {len(delays)}; "
            f"до первого действия медиана {format_delay(statistics.median(delays))}, "
            f"p90 {format_delay(delays[math.ceil(0.9 * len(delays)) - 1])}")


def park_restart_notice(book: Book, agent: str) -> str:
    """One-shot recovery on any MCP tool response after the server restarts.

    Both the park intent and the originating server epoch live in jobs.json;
    no in-process wait or browser tab is needed to recover this instruction.
    An intentional same-process parking limit is never revived.
    """
    snapshot = lookup(book, agent)
    if (not snapshot or not snapshot.get("park_wanted")
            or snapshot.get("state") not in ("parked", "answered-unparked")
            or snapshot.get("park_epoch") == _PARK_EPOCH):
        return ""
    with book.locked() as jobs:
        job = jobs.get(norm_id(agent))
        if (not job or not job.get("park_wanted")
                or job.get("state") not in ("parked", "answered-unparked")
                or job.get("park_epoch") == _PARK_EPOCH):
            return ""
        job["state"] = "parked"
        job["park_started"] = _now()  # restart gives a fresh, bounded park lease
        job["park_epoch"] = _PARK_EPOCH
        job["updated"] = _now()
    return ("⟦быстрый агент: восстановление парковки после перезапуска⟧ "
            "Ты запаркован — СРАЗУ вызови fleet_quick(agent='"
            + str(agent).upper() + "', command='park'). "
            "Очередь say будет доставлена этим вызовом.")


def pause_alarms(book: Book, *, now: datetime | None = None,
                 threshold_s: float = 600.0, send_threshold_s: float = 300.0,
                 actions: dict[str, str] | None = None) -> list[str]:
    """Live first-action and unsent-message alerts, with extension retry reason."""
    current = now or datetime.now(timezone.utc)
    result = []
    actions = actions if actions is not None else first_actions(book.dir.parents[2] / "logs",
                                                                 book.jobs().values())
    for job in book.jobs().values():
        if job.get("state") in ("closed", "waiting"):
            continue
        pause = job.get("send_pause") or {}
        if not isinstance(pause, dict):
            pause = {}
        reason = pause.get("reason") or "причина в журнале расширения не указана"
        queued = _parse_time(job.get("outbox_since_at") or job.get("new_at") or job.get("created"))
        if job.get("outbox") and queued is not None:
            elapsed = (current - queued).total_seconds()
            if elapsed >= send_threshold_s:
                result.append(f"{job['id']}: ждёт отправки {int(elapsed // 60)} мин; "
                              f"причина: {reason}")
        started = _parse_time(job.get("new_at") or job.get("created"))
        if (started is not None and (current - started).total_seconds() >= threshold_s
                and not (job.get("first_action_at") or actions.get(job.get("tag", "")))):
            result.append(f"{job['id']}: нет первого действия "
                          f"{int((current - started).total_seconds() // 60)} мин после new; "
                          f"причина: {reason}")
        if pause:
            since = _parse_time(pause.get("since"))
            if since is not None and (current - since).total_seconds() >= threshold_s:
                result.append(f"{job['id']}: отправка на паузе "
                              f"{int((current - since).total_seconds() // 60)} мин; "
                              f"до {pause.get('until') or 'неизвестно'}; причина {reason}")
    return result


def wake_silent(book: Book, recent: dict[str, bool] | None = None, *,
                silence_s: dict[str, float] | None = None,
                now: datetime | None = None, wake_after_s: float = WAKE_SILENCE_S,
                max_orders: int = WAKE_MAX) -> int:
    """Queue at most two browser wakes for silent, non-generating quick agents."""
    current = now or datetime.now(timezone.utc)
    count = 0
    recent = recent or {}
    # Skip disk writes on the common (no wake due) launcher poll.
    candidates = book.jobs().values()
    def due(job: dict[str, Any]) -> bool:
        if job.get("state") not in ("new", "working"):
            return False
        if job.get("outbox") or job.get("wake_pending") or job.get("generating"):
            return False
        tag = str(job["tag"]).upper()
        if recent.get(tag) or int(job.get("wake_count") or 0) >= max_orders or not job.get("url"):
            return False
        last = _parse_time(job.get("updated") or job.get("created"))
        silence = (silence_s.get(tag) if silence_s is not None else None)
        if silence is None:
            silence = (current - last).total_seconds() if last else 0
        return silence >= wake_after_s
    if not any(due(job) for job in candidates):
        return 0
    with book.locked() as jobs:
        for job in jobs.values():
            if not due(job):
                continue
            job["outbox"].append(
                "Продолжай задание. Если закончил — сдай полный итог вызовом "
                f"fleet_quick(agent='{job['tag']}', command='report', args=['итог'])."
            )
            job["outbox_since_at"] = _now()
            job["wake_pending"] = True
            job["wake_count"] = int(job.get("wake_count") or 0) + 1
            job["updated"] = _now()
            count += 1
    return count


# ------------------------------------------------------------------ расширение
def _ready_dependents(jobs: dict[str, Any]) -> list[str]:
    """Pure book-only check; no browser or database access on an ordinary tick."""
    return [j["id"] for j in jobs.values() if j.get("state") == "waiting" and
            all((parent := jobs.get(dep)) is not None and
                (parent.get("answers", 0) > 0 or parent.get("state") == "closed")
                for dep in j.get("after") or [])]


def mark_acting(book: Book, job: dict[str, Any] | None) -> bool:
    """17.11: агент уже вызывает инструменты dev-mcp, а книга держит его [new] с «ждёт отправки»
    (квитанция расширения не пришла) — оператор закрывал живые чаты и дублировал работу.
    Первое действие агента и есть доказательство доставки первого сообщения."""
    if not job or job.get("state") != "new" or not job.get("outbox"):
        return False
    with book.locked() as jobs:
        current = jobs.get(job["id"])
        if not current or current.get("state") != "new" or not current.get("outbox"):
            return False
        text = current["outbox"].pop(0)
        now = _now()
        current.setdefault("messages", []).append({"role": "owner", "text": text, "ts": now})
        current["state"] = "working"
        current["answer_source"] = ""
        current["delivery_proof"] = "agent-action"
        current["first_delivered_at"] = current.get("first_delivered_at") or now
        current["outbox_since_at"] = now if current["outbox"] else ""
        current["updated"] = now
    return True


ABANDON_S = 12 * 3600
_ANSWERED = ("answered", "answered-unparked", "parked")


def _abandoned(job: dict[str, Any], now: datetime) -> bool:
    """Ответ сдан, заказчик (сессия Claude Code/ведущий) не пишет и не закрывает 12 ч."""
    if job.get("state") not in _ANSWERED or job.get("outbox") or job.get("inbox"):
        return False
    seen = _parse_time(job.get("updated") or job.get("answered_at"))
    return bool(seen and seen.tzinfo and (now - seen).total_seconds() >= ABANDON_S)


def close_abandoned(book: Book, *, now: datetime | None = None) -> list[str]:
    """17.11: «ответили, ждут владельца» копились от закончившихся сессий навсегда.
    Ответ остаётся в книге (quick show), вкладка закрывается обычным close-приказом."""
    now = now or datetime.now(timezone.utc)
    if not any(_abandoned(j, now) for j in book.jobs().values()):
        return []
    closed = []
    with book.locked() as jobs:
        for job in jobs.values():
            if _abandoned(job, now):
                job["state"] = "closed"
                job["closed_at"] = job["updated"] = _now()
                job["closed_reason"] = "заказчик не забрал ответ 12 ч"
                closed.append(job["id"])
    return closed


def orders(book: Book) -> list[dict[str, Any]]:
    """Что расширению делать: открыть чат, отправить outbox, закрыть. Waiting не занимает слот."""
    close_abandoned(book)
    jobs = book.jobs()
    if _ready_dependents(jobs):
        with book.locked() as current:
            for jid in _ready_dependents(current):
                job = current[jid]
                context = []
                for dep in job["after"]:
                    parent = current[dep]
                    answer = parent.get("last_answer") or "(закрыт без ответа)"
                    context.append(f"### {dep} (последний ответ)\n{answer[:6000]}")
                job["outbox"][0] += ("\n\nОтветы предыдущих быстрых агентов:\n\n" +
                                     "\n\n".join(context))
                job["state"] = "new"
                job["updated"] = _now()
                job["new_at"] = job["updated"]
                job["outbox_since_at"] = job["updated"]
            jobs = current
    out = []
    for j in jobs.values():
        if j["state"] == "waiting":
            continue
        if j["state"] == "closed":
            # Re-issue for 24 h even after an earlier tab_closed ack: a reloaded
            # extension can lose bindings for discarded tabs, but still matches URL.
            closed_at = _parse_time(j.get("closed_at") or j.get("updated"))
            if (j.get("url") and closed_at is not None and closed_at.tzinfo is not None
                    and 0 <= (datetime.now(timezone.utc) - closed_at).total_seconds() < CLOSE_RETRY_S):
                out.append({"id": j["id"], "label": f"pult-{j['id']}",
                            "url": j["url"], "close": True})
            continue
        pending = bool(j.get("outbox"))
        wake = bool(j.get("wake_pending"))
        out.append({"id": j["id"], "agentTag": j["tag"],
                    "label": f"pult-{j['id']}", "url": j.get("url") or "",
                    "home": j.get("home") or "", "send": (j.get("outbox") or [None])[0],
                    "pending": len(j.get("outbox") or []), "wake": wake,
                    "idle": not pending and not wake, "open": pending or wake,
                    "status": j["state"], "closeAfterSend": True,
                    "close_after_send": True,
                    "handoffOldUrl": (j.get("chat_handoff") or {}).get("old_url", "")
                        if (j.get("chat_handoff") or {}).get("status") == "pending" else ""})
    return out


def report(book: Book, data: dict[str, Any], *,
           deliver: Callable[[dict[str, Any], str], None] | None = None) -> dict[str, Any]:
    """Расширение: `sent` (ушло первое из outbox), `url` (адрес чата), `answer` (законченный ход),
    `tab_closed`. Новый ответ — владельцу сразу."""
    jid = norm_id(str(data.get("id") or ""))
    prior = book.jobs().get(jid)
    action_at = (first_actions(book.dir.parents[2] / "logs", [prior])
                 .get(str(prior.get("tag") or ""), "") if prior and data.get("answer")
                 and str(data["answer"]).strip() != prior.get("last_answer")
                 and prior.get("answer_source") != "mcp"
                 and not prior.get("first_action_at") else "")
    fresh = None
    with book.locked() as jobs:
        job = jobs.get(jid)
        if job is None:
            return {"ok": False, "why": "unknown"}
        if data.get("url") and not job.get("url"):
            incoming = str(data["url"])[:300]
            handoff = job.get("chat_handoff") or {}
            if not handoff or handoff.get("old_url", "").rstrip("/") != incoming.rstrip("/"):
                job["url"] = incoming
                job["chat_opened_at"] = job.get("chat_opened_at") or _now()
        handoff = job.get("chat_handoff") or {}
        # Browser may learn the new URL *before* the first message is acknowledged.
        # Revoke the source only once an actual successor send is confirmed.
        if (handoff.get("status") == "pending" and data.get("sent") is True
                and job.get("url") and job["url"].rstrip("/") != handoff["old_url"].rstrip("/")):
            handoff["status"] = "activated"
            handoff["successor_url"] = job["url"]
            handoff["activated_at"] = _now()
        # Fleet-tabs 0.19.29+ sends these on retry backoff. Bound all
        # untrusted extension fields before persisting in the shared book.
        if data.get("sent") is True or data.get("send_paused") is False:
            job.pop("send_pause", None)
        elif data.get("send_paused") is True:
            previous = job.get("send_pause") or {}
            if not isinstance(previous, dict):
                previous = {}
            until = str(data.get("send_pause_until") or "")[:40]
            parsed = _parse_time(until)
            if parsed is None or parsed.tzinfo is None:
                until = ""
            reason = re.sub(r"[\r\n\t]+", " ", str(
                data.get("send_pause_reason") or ""))[:180].strip()
            try:
                failures = max(0, min(1000, int(data.get("send_failures") or 0)))
            except (ValueError, TypeError, OverflowError):
                failures = 0
            job["send_pause"] = {
                "since": previous.get("since") or _now(),
                "until": until,
                "reason": reason,
                "failures": failures,
            }
        if data.get("sent") and job.get("outbox"):
            text = job["outbox"].pop(0)
            now = _now()
            job["messages"].append({"role": "owner", "text": text, "ts": now})
            job["first_delivered_at"] = job.get("first_delivered_at") or now
            job["outbox_since_at"] = now if job["outbox"] else ""
            if job.get("state") != "parked" and not (job.get("state") == "answered-unparked"
                                                     and job.get("park_wanted")):
                job["state"] = "working"
            job["wake_pending"] = False
            job["tab_closed"] = False
            if job.get("state") == "working":
                job["answer_source"] = ""
        if "generating" in data:
            job["generating"] = bool(data["generating"])
        if data.get("tab_closed"):
            job["tab_closed"] = True
        answer = str(data.get("answer") or "").strip()
        if (answer and answer != job.get("last_answer") and job["state"] != "closed"
                and job.get("answer_source") != "mcp"):
            job["last_answer"] = answer[-60_000:]
            job["answers"] += 1
            job["answered_at"] = _now()
            job["reported_at"] = job.get("reported_at") or job["answered_at"]
            job["first_action_at"] = job.get("first_action_at") or action_at
            job["messages"].append({"role": "agent", "text": answer[-60_000:], "ts": _now()})
            job["state"] = "working" if job.get("outbox") else "answered"
            fresh = dict(job)
        job["messages"] = job["messages"][-40:]
        job["updated"] = _now()
    if fresh is not None and deliver is not None:
        try:
            deliver(fresh, fresh["last_answer"])
            with book.locked() as jobs:
                jobs[jid]["notified_at"] = jobs[jid].get("notified_at") or _now()
        except Exception:  # noqa: BLE001 — доставка не должна терять ответ: он уже в книге
            pass
    return {"ok": True, "new_answer": fresh is not None}


def render(job: dict[str, Any], history: dict[str, Any] | None = None, *, full: bool = True) -> str:
    since = job.get("answered_at") or job.get("updated") or job.get("created")
    pending = ""
    if job.get("state") == "new" and job.get("outbox"):
        queued = _parse_time(job.get("outbox_since_at") or job.get("new_at") or job.get("created"))
        minutes = max(0, int((datetime.now(timezone.utc) - queued).total_seconds() // 60)) if queued and queued.tzinfo else 0
        pause = job.get("send_pause") or {}
        reason = pause.get("reason") if isinstance(pause, dict) else ""
        pending = f"; ждёт отправки {minutes} мин; последняя причина: {reason or 'не указана'}"
    lines = [f"{job['id']} [{job['state']}] владелец {job['owner']}; проект {job.get('project') or '—'}; "
             f"ответов {job['answers']}; чат {job.get('url') or 'адрес не получен'}{pending}",
             f"создан {job['created'][:16]}, последнее изменение {str(job['updated'])[:16]}"
             + (f", ждёт отправки: {len(job['outbox'])}" if job.get("outbox") else "")]
    pause = job.get("send_pause")
    if isinstance(pause, dict):
        lines.append(f"пауза отправки до {pause.get('until') or 'неизвестно'}, "
                     f"причина {pause.get('reason') or 'не указана'}"
                     f"; ошибок {pause.get('failures') or 0}")
    stamps = [job.get("new_at") or job.get("created"), job.get("chat_opened_at"),
              job.get("first_delivered_at"), job.get("first_action_at"),
              job.get("reported_at") or job.get("answered_at"), job.get("notified_at")]
    labels = ("new", "чат", "доставлено", "первое действие", "report", "уведомлено")
    base = _parse_time(stamps[0])
    delays = []
    for name, stamp in zip(labels, stamps):
        ts = _parse_time(stamp)
        delta = (ts - base).total_seconds() if base and ts else None
        delays.append(f"{name} {format_delay(delta) if delta is not None else '—'}")
    lines.append("задержки: " + " · ".join(delays))
    if history:
        lines.append(f"действий агента в dev-mcp: {history.get('count', 0)}"
                     + ("; последние: " + " | ".join(history.get("actions", [])[-5:]) if history.get("actions") else ""))
    if job.get("last_answer"):
        text = job["last_answer"] if full else job["last_answer"][-1500:]
        lines += [f"— последний ответ ({str(since)[:16]}):", text]
    return "\n".join(lines)


def operator_roots(job: dict[str, Any] | None) -> tuple[Path, ...]:
    """Область оператора открытого задания Claude Code: его явный scope (абсолютные пути)."""
    if not job or not (job.get("owner") == "claude" or
                       str(job.get("owner", "")).startswith("claude:")) or job.get("state") not in OPEN_STATES:
        return ()
    out = []
    for s in job.get("scope") or []:
        p = Path(str(s))
        if not p.is_absolute():
            if not job.get("root"):
                continue
            p = Path(job["root"]) / p
        out.append(Path(os.path.abspath(p)))
    return tuple(out)


def scope_denial(job: dict[str, Any], abs_path_norm: str) -> str:
    """Запись быстрого агента вне корня/области владельца — отказ (права не шире владельца)."""
    from .model import norm_path

    roots = [norm_path(str(Path(job["root"]) / s)) if job.get("root") and not Path(s).is_absolute()
             else norm_path(s) for s in (job.get("scope") or [])]
    if not roots and job.get("root"):
        roots = [norm_path(job["root"])]
    if not roots:
        return ""
    for r in roots:
        r = r.rstrip("/")
        if abs_path_norm == r or abs_path_norm.startswith(r + "/"):
            return ""
    return (f"Быстрый агент {job['tag']} пишет только в области владельца: {', '.join(roots)}. "
            "Нужно шире — скажи об этом в ответе, владелец расширит.")
