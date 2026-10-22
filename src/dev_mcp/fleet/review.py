from __future__ import annotations

import json
import ntpath
import re
import shlex
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from .model import norm_path, paths_overlap

_ACCESS_KINDS = {"access", "bin", "root", "network"}
_REVIEW_PREFIX = "[fleet-review] "


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json_list(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return ()
    return tuple(str(x) for x in value) if isinstance(value, (list, tuple)) else ()


# Сколько исправлений подряд по одной задаче идут обычным порядком. Следующее — «финальный
# круг»: его ревью при зелёной приёмке не может отклонить работу, замечание уходит отдельной
# задачей (follow-up). До 25.09.2026 здесь fix вставал в draft и ждал владельца — а владелец
# очередь не разбирает, и семья задач (с зависящими от неё ТЗ) стояла навсегда.
MAX_FIX_ROUNDS = 2
# После стольких fix подряд с КРАСНОЙ приёмкой микро-ремонт считается неэффективным.
# Семью НЕ замораживаем: следующий fix становится diagnostic/end-to-end repair и обязан
# разобрать накопленные причины целиком. Ручного owner-gate в review lifecycle нет.
MAX_FIX_ROUNDS_HARD = 5

FINAL_MARK = "[fleet-final]"
FINAL_REVIEW_NOTE = (
    f"{FINAL_MARK} Финальное ревью: повторная обычная эскалация недоступна. Прошлый ревьюер "
    "не смог проверить сам — используй прогон приёмки, файлы, снимки через read_file и логи. "
    "accept — если приёмка зелёная и пункты ТЗ по доступным свидетельствам выполнены; "
    "непроверенное вживую укажи в note. reject — только с проверенным дефектом работы. "
    "Если проверку блокирует сама платформа/инструмент, а не дефект автора, используй "
    "escalate и note='system-blocker: <точная команда, ошибка, как воспроизвести>'. "
    "Ревью возобновится только по подтверждённому событию новой выкатки платформы; "
    "ни красная приёмка, ни таймер не дают права его разморозить."
)
FINAL_FIX_NOTE = (
    f"{FINAL_MARK} Финальный круг исправлений: сделай то, что требует ТЗ и последнее замечание, "
    "а спорный край (что ТЗ прямо не задаёт) опиши в «Итог» как вне задачи. Ревью этого fix при "
    "зелёной приёмке примет работу, а остаток замечаний станет отдельной задачей."
)
HARD_FIX_NOTE = (
    "Повторные локальные патчи не закрыли outcome. Это diagnostic/end-to-end repair: перед правкой "
    "прочитай историю review/fix всей семьи, сгруппируй повторяющиеся причины reject, найди общий "
    "корень и закрой их одним согласованным изменением. Не проси owner approve и не дроби найденные "
    "дефекты на новые микро-ТЗ, если они относятся к тому же outcome."
)


def _next_tz(tx: Any, project: str, base: str) -> str:
    candidate = base
    index = 2
    while tx.one("SELECT tz FROM tasks WHERE project=? AND tz=?", (project, candidate)):
        candidate = f"{base}:{index}"
        index += 1
    return candidate


def _marker(project: str, tz: str) -> str:
    return _REVIEW_PREFIX + json.dumps(
        {"project": project, "tz": tz},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _review_target(review_tz: str, body: str, project: str) -> tuple[str, str]:
    """Use a server-authored marker only when it matches this review's identity.

    >>> _review_target('review:TZ-1', _marker('P', 'TZ-1'), 'P')
    ('P', 'TZ-1')
    >>> _review_target('review:triage:18:6', _marker('P', 'triage:18'), 'P')
    ('P', 'triage:18')
    >>> _review_target('review:TZ-1:2', _marker('P', 'TZ-1'), 'P')
    ('P', 'TZ-1')
    >>> _review_target('review:TZ-1', 'Legacy review without a marker', 'P')
    ('P', 'TZ-1')
    >>> _review_target('review:TZ-1', '[fleet-review] not JSON', 'P')
    Traceback (most recent call last):
    ...
    ValueError: review:TZ-1: invalid review target marker
    >>> _review_target('review:TZ-1:2', _marker('P', 'TZ-2'), 'P')
    Traceback (most recent call last):
    ...
    ValueError: review:TZ-1:2: marker target does not match review id
    >>> _review_target('review:TZ-1', _marker('P', 'TZ-2'), 'P')
    Traceback (most recent call last):
    ...
    ValueError: review:TZ-1: marker target does not match review id
    >>> _review_target('review:TZ-1', _marker('OTHER', 'TZ-1'), 'P')
    Traceback (most recent call last):
    ...
    ValueError: review:TZ-1: marker project does not match review project
    """
    if not review_tz.startswith("review:"):
        raise ValueError(f"{review_tz}: review target is missing")
    first = (body or "").splitlines()[0].strip() if body else ""
    if first.startswith("[fleet-review]"):
        try:
            data = json.loads(first[len(_REVIEW_PREFIX) :]) if first.startswith(_REVIEW_PREFIX) else None
        except (ValueError, TypeError):
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("tz"), str) or not data["tz"]:
            raise ValueError(f"{review_tz}: invalid review target marker")
        if data.get("project") != project:
            raise ValueError(f"{review_tz}: marker project does not match review project")
        source = data["tz"]
        name = review_tz[len("review:") :]
        # A review may be named review:<tz>:2, review:<tz>:3, ...; a
        # source tz can itself end in a number (e.g. triage:18).
        if name != source:
            suffix = name[len(source) + 1 :] if name.startswith(source + ":") else ""
            if not suffix.isascii() or not suffix.isdecimal() or int(suffix or "0") < 2 or str(int(suffix)) != suffix:
                raise ValueError(f"{review_tz}: marker target does not match review id")
        return project, source
    target = re.sub(r":\d+$", "", review_tz[len("review:") :])
    if not target:
        raise ValueError(f"{review_tz}: review target is missing")
    return project, target


def _fix_source(body: str, project: str) -> tuple[str, str] | None:
    first = (body or "").splitlines()[0].strip() if body else ""
    prefix = "[fleet-fix] source="
    if not first.startswith(prefix):
        return None
    address = first[len(prefix):].strip()
    if "/" not in address:
        return project, address
    source_project, source_tz = address.rsplit("/", 1)
    if not source_project or not source_tz:
        return None
    return source_project, source_tz


def _blind_submission_sealed(tx: Any, original: Any) -> bool:
    """A mechanical review-check may not replace an independent server seal.

    The author claim is released at submit, so the regular live-claim briefing
    check cannot be used here. Bind the most recent author claim, its immutable
    blind_seal event and the current blind bytes to the same attempt instead.
    """
    project, tz = str(original["project"]), str(original["tz"])
    if project != "Mill Works":
        return True
    plain = re.fullmatch(r"TZ-MR-batch-([a-z0-9]{6,40})", tz)
    retry = re.fullmatch(r"fix:TZ-MR-batch-([a-z0-9]{6,40})(?::[1-9][0-9]*)?", tz)
    if plain is None and retry is None:
        return True
    author = str(original["author_worker"] or "")
    if not author:
        return False
    claim = tx.one(
        "SELECT id FROM claims WHERE worker_id=? AND project=? AND tz=? "
        "ORDER BY id DESC LIMIT 1", (author, project, tz),
    )
    if claim is None:
        return False
    attempt_id = f"claim-{int(claim['id'])}" if retry is not None else None
    batch_id = "batch-" + (retry or plain).group(1)
    seal = tx.one(
        "SELECT payload FROM events WHERE kind='blind_seal' AND "
        "worker_id=? AND project=? AND tz=? ORDER BY id DESC LIMIT 1",
        (author, project, tz),
    )
    if seal is None:
        return False
    try:
        data = json.loads(seal["payload"] or "{}")
        if (not isinstance(data, dict) or data.get("claim_id") != int(claim["id"])
                or data.get("batch_id") != batch_id
                or data.get("attempt_id") != attempt_id):
            return False
        from .briefing import _current_blind_digest

        root = tx.one("SELECT root FROM projects WHERE name=?", (project,))
        return (root is not None and isinstance(data.get("seal_sha256"), str)
                and _current_blind_digest(str(root["root"]), batch_id, attempt_id)
                == data["seal_sha256"])
    except (ValueError, TypeError, OSError, KeyError):
        return False


def _review_body(row: Any) -> str:
    acceptance = _json_list(row["acceptance"])
    lines = [
        _marker(str(row["project"]), str(row["tz"])),
        f"Проверить: {row['project']}/{row['tz']}.",
        f"Автор: {row['author_worker'] or 'не указан'}. Ревью своей работы запрещено.",
        f"Итог автора: {row['spec_path'] or '(ТЗ без файла)'} → раздел «Итог».",
        "Accept требует собственный успешный run каждой acceptance-команды.",
    ]
    lines.extend(f"- {cmd}" for cmd in acceptance)
    if not acceptance:
        lines.append("- (acceptance-команды не заданы)")
    for cmd in sorted(_gate_commands(str(row["project"]), acceptance)):
        lines.append(
            f"`{cmd}` — гейт проекта: зелёный серверный прогон автора по тем же файлам зоны засчитан "
            "и тебе (accept скажет, если файлы с тех пор менялись). Читай правку, не гоняй гейт заново."
        )
    return "\n".join(lines)


def open_review(store: Any, project: str, tz: str, *, final_note: str = "") -> str:
    """Create an idempotent ready review task for a submitted work item.

    final_note — ревью после escalate: другой агент, без права на escalate (см. decide).
    """
    if not final_note:
        from dev_mcp.intents.stages import request_server_stage_check
        pending = request_server_stage_check(store,project,tz)
        if pending is not None: return pending
    with store.tx() as tx:
        return _open_review_tx(tx, project, tz, final_note=final_note)


def _open_review_tx(tx: Any, project: str, tz: str, *, final_note: str = "") -> str:
    original = tx.one("SELECT * FROM tasks WHERE project=? AND tz=?", (project, tz))
    if original is None:
        raise ValueError(f"{project}/{tz}: task not found")
    if original["state"] != "submitted":
        raise ValueError(f"{project}/{tz}: review requires state=submitted")

    for row in tx.query(
        "SELECT tz,body FROM tasks WHERE project=? AND kind='review' "
        "AND state IN ('ready','taken','submitted') ORDER BY created_at,tz",
        (project,),
    ):
        try:
            target = _review_target(str(row["tz"]), str(row["body"] or ""), project)
        except ValueError:
            continue
        if target == (project, tz):
            return str(row["tz"])

    review_tz = _next_tz(tx, project, f"review:{tz}")
    now = _now()
    body = _review_body(original)
    if final_note:
        body = body + "\n" + FINAL_REVIEW_NOTE + "\nПочему escalate: " + final_note.strip()[:1500]
    elif FINAL_MARK in str(original["body"] or ""):
        body = body + "\n" + FINAL_MARK + " Это ревью финального круга исправлений: при зелёной " \
            "приёмке reject превратится в accept, а твоё замечание — в отдельную задачу."
    tx.change(
        "INSERT INTO tasks("
        "project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,"
        "resources,acceptance,author_worker,body,created_at,updated_at"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            project,
            review_tz,
            f"Review {tz}",
            "ready",
            int(original["priority"]),
            "review",
            str(original["spec_path"] or ""),
            str(original["spec_sha"] or ""),
            "[]",
            "[]",
            # 18.10: ревью без ресурсов исходной задачи не может повторить её live-приёмку
            # (стенд, телефон, VM) и честно блокировалось «resources=(нет)» по кругу.
            str(original["resources"] or "[]"),
            str(original["acceptance"] or "[]"),
            original["author_worker"],
            body,
            now,
            now,
        ),
        event={
            "kind": "review_open",
            "worker_id": original["author_worker"],
            "project": project,
            "tz": review_tz,
            "payload": {"review_of": tz},
        },
    )
    return review_tz


def _norm_command(value: str) -> str:
    text = " ".join(str(value).replace("\\", "/").split()).casefold()
    parts = text.split()
    # Acceptance historically stored Windows batch invocations as
    # "cmd.exe /d /c check.cmd ...", while run now safely executes the same
    # project-local batch directly. Treat those spellings as the same command.
    if parts and parts[0] in {"cmd", "cmd.exe"}:
        parts = parts[1:]
        while parts and parts[0] in {"/d", "/s"}:
            parts = parts[1:]
        if parts and parts[0] in {"/c", "/k"}:
            parts = parts[1:]
        if parts and parts[0] == "call":
            parts = parts[1:]
    if parts and parts[0].startswith("./"):
        parts[0] = parts[0][2:]
    return " ".join(parts)


def _acceptance_argv(command: str | tuple[str, ...]) -> tuple[str, ...] | None:
    """Tokenize Windows command text without dividing quoted path operands.

    The run event contains argv rendered as command text. Backslashes first
    become path separators so shlex does not treat them as escapes; only
    Windows-style double quotes delimit one operand. Preserve whitespace
    *inside* the quotes rather than normalizing it as argument separators.
    """
    if isinstance(command, tuple):
        # Server-recorded argv already has trusted argument boundaries. Joining
        # it first loses quoted pytest -k expressions and paths with spaces.
        parts = [part.replace("\\", "/").casefold() for part in command]
    else:
        lexer = shlex.shlex(str(command).replace("\\", "/"), posix=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        lexer.quotes = '"'
        lexer.escape = ""
        try:
            parts = [part.casefold() for part in lexer]
        except ValueError:
            return None
    if parts and parts[0] in {"cmd", "cmd.exe"}:
        parts = parts[1:]
        while parts and parts[0] in {"/d", "/s"}:
            parts = parts[1:]
        if parts and parts[0] in {"/c", "/k"}:
            parts = parts[1:]
        if parts and parts[0] == "call":
            parts = parts[1:]
    if parts and parts[0].startswith("./"):
        parts[0] = parts[0][2:]
    return tuple(parts)


def _matches_acceptance_run(required: str, observed: str | tuple[str, ...], run_cwd: str,
                            expected_cwd: str = "") -> bool:
    """Match an absolute acceptance operand to a safe relative run operand.

    `run_cwd` comes from the server-recorded run event path, never from the
    agent's argv.  Only paths inside that directory may be made equivalent;
    unrelated arguments, drive-relative paths and traversal never are.
    Preserve historical exact matches, including events without a cwd.
    """
    target, actual = _acceptance_argv(required), _acceptance_argv(observed)
    if not target or not actual:
        return False
    if target == actual:
        # An identical relative argv can execute a different verifier in a
        # foreign directory. Bind it to the server's claim/project directory.
        # Fully absolute source operands retain their explicit path identity;
        # old events with no recorded cwd retain the historical contract.
        operands = [p for p in target[1:] if not p.startswith('-') and
                    ('/' in p or '\\' in p or p.endswith(('.py','.cmd','.bat','.js','.mjs','.json','.md')))]
        relative = not operands or any(not ntpath.isabs(p) for p in operands)
        relative = relative or (target[0].endswith(('.cmd','.bat','.py','.js','.mjs'))
                                and not ntpath.isabs(target[0]))
        if relative and expected_cwd and run_cwd and norm_path(run_cwd) != norm_path(expected_cwd):
            return False
        return True
    if not run_cwd or len(target) != len(actual):
        return False
    cwd = ntpath.normcase(ntpath.normpath(str(run_cwd).replace("/", "\\")))
    if not ntpath.isabs(cwd) or not ntpath.splitdrive(cwd)[0]:
        return False

    changed = False
    for expected, supplied in zip(target, actual):
        if expected == supplied:
            continue
        if (not ntpath.isabs(expected) or ntpath.isabs(supplied)
                or ntpath.splitdrive(supplied)[0]
                or ".." in supplied.replace("\\", "/").split("/")):
            return False
        try:
            resolved = ntpath.normcase(ntpath.normpath(ntpath.join(cwd, supplied)))
            if (ntpath.commonpath([cwd, resolved]) != cwd
                    or resolved != ntpath.normcase(ntpath.normpath(expected))):
                return False
        except ValueError:  # Cross-drive comparison or invalid path.
            return False
        changed = True
    return changed


def _payload_commands(payload: Any) -> list[str | tuple[str, ...]]:
    if isinstance(payload, str):
        try:
            return _payload_commands(json.loads(payload))
        except (json.JSONDecodeError, TypeError):
            return [payload] if payload.strip() else []
    if isinstance(payload, dict):
        out: list[str | tuple[str, ...]] = []
        for key in ("command", "cmd", "argv"):
            if key in payload:
                out.extend(_payload_commands(payload[key]))
        return out
    if isinstance(payload, (list, tuple)):
        if payload and all(isinstance(x, (str, int, float)) for x in payload):
            return [tuple(str(x) for x in payload)]
    return []


def _payload_tree(payload: Any) -> str | None:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return None
    return str(payload.get("tree")) if isinstance(payload, dict) and payload.get("tree") else None


def _expected_run_cwd(tx: Any, worker_id: str, since: str) -> str:
    held = tx.one(
        "SELECT c.id,c.project,t.kind,p.root FROM claims c JOIN projects p ON p.name=c.project "
        "JOIN tasks t ON t.project=c.project AND t.tz=c.tz "
        "WHERE c.worker_id=? AND c.state='active' AND c.started_at=? ORDER BY c.id DESC LIMIT 1",
        (worker_id, since))
    if not held:
        return ''
    root = str(held['root'])
    if held['kind'] != 'review':
        take = tx.one("SELECT payload FROM events WHERE kind='take' AND worker_id=? "
                      "AND json_extract(payload,'$.claim_id')=? ORDER BY id DESC LIMIT 1",
                      (worker_id,int(held['id'])))
        try:
            context = json.loads(take['payload']).get('claim_context',{}) if take else {}
            if (context.get('mode')=='isolated' and context.get('claim_id')==held['id']
                    and context.get('worker_id')==worker_id and context.get('project')==held['project']
                    and isinstance(context.get('tree_path'),str)):
                root=context['tree_path']
        except (ValueError, TypeError, AttributeError):
            pass
    return root


def _missing_runs(
    tx: Any, worker_id: str, since: str, commands: Iterable[str], tree: str | None = None
) -> list[str]:
    """Acceptance-команды без собственного успешного прогона.

    С `tree` засчитываются только прогоны по тому же состоянию файлов зоны, что и
    сейчас: прогон, после которого файлы правили, доказывает уже не то, что
    принимается.
    """
    required = tuple(commands)
    if not required:
        return []
    deferred = deferred_gate_commands(tx, worker_id, since, required)
    required = tuple(cmd for cmd in required if cmd not in deferred)
    if not required:
        return []
    expected_cwd = _expected_run_cwd(tx,worker_id,since)
    successful: list[tuple[str | tuple[str, ...], str]] = []
    for row in tx.query(
        "SELECT payload,rc,path_norm FROM events WHERE worker_id=? AND kind='run' AND ts>=? ORDER BY id",
        (worker_id, since),
    ):
        if row["rc"] != 0:
            continue
        if tree is not None and _payload_tree(row["payload"]) != tree:
            continue
        for command in _payload_commands(row["payload"]):
            successful.append((command, str(row["path_norm"] or "")))
    return [cmd for cmd in required if not any(
        _matches_acceptance_run(cmd, observed, cwd, expected_cwd) for observed, cwd in successful
    )]


def _gate_commands(project: str, acceptance: Iterable[str]) -> set[str]:
    """Команды приёмки, совпадающие с гейтом проекта (fleet-projects.toml)."""
    try:
        from . import goals

        gate = str(goals.project_info(project).get("gate") or "").strip()
    except Exception:
        return set()
    return {cmd for cmd in acceptance if gate and _norm_command(cmd) == _norm_command(gate)}


def deferred_gate_commands(tx: Any, worker_id: str, since: str, acceptance: Iterable[str]) -> set[str]:
    """Гейт проекта в приёмке задачи, пока ствол красный, — откладывается до интеграции.

    П.3 плана консорциума 11.10: цвет ствола держит интеграцию, а не вердикт. Но гейт,
    вписанный в приёмку задачи, требовал rc=0 у автора и ревьюера — при стволе, красном
    по чужим тестам, такая задача не принималась никогда (Beta App 14–17.10: 34 задачи,
    ревью блокировались «system-blocker: check_all rc=1 вне зоны»). После accept гейт всё
    равно гоняет интеграция, новая поломка станет стыком. Не откладывается: стык (его цель —
    зелёный гейт) и задача, чья зона пересекает красную зону ствола.
    """
    held = tx.one(
        "SELECT c.project,c.tz,t.kind,t.body FROM claims c JOIN tasks t "
        "ON t.project=c.project AND t.tz=c.tz WHERE c.worker_id=? AND c.started_at=? "
        "ORDER BY c.id DESC LIMIT 1",
        (worker_id, since),
    )
    if held is None:
        return set()
    project = str(held["project"])
    gate_cmds = _gate_commands(project, acceptance)
    if not gate_cmds:
        return set()
    last = tx.one(
        "SELECT rc FROM events WHERE kind='gate' AND project=? ORDER BY id DESC LIMIT 1", (project,)
    )
    if last is None or not last["rc"]:
        return set()
    source_tz = str(held["tz"])
    if str(held["kind"]) == "review":
        project, source_tz = _review_target(source_tz, str(held["body"] or ""), project)
    if source_tz.split(":", 1)[0] in ("styk", "regate") or ":styk:" in source_tz:
        return set()
    source = tx.one("SELECT owns FROM tasks WHERE project=? AND tz=?", (project, source_tz))
    try:
        from .queue import red_zone

        zone, _ = red_zone(tx, project)
    except Exception:
        return set()
    owns = _json_list(source["owns"]) if source is not None else []
    if any(paths_overlap(str(a), str(b)) for a in owns for b in zone):
        return set()
    return gate_cmds


def _shared_proof(tx: Any, project: str, command: str, tree: str | None) -> bool:
    """Успешный серверный прогон гейта проекта по ЭТОМУ ЖЕ состоянию файлов зоны — чей угодно.

    Гейт Beta App идёт ~880 с: автор, ревьюер и интегратор гоняли его трижды, а прогон
    ревьюера по тому же дереву ничего нового не доказывает — rc пишет сервер, не агент.
    Независимость ревью держится на чтении правки; короткие команды ревьюер гоняет сам.
    Без хеша дерева — не засчитывается.
    """
    if not tree:
        return False
    registered = tx.one('SELECT root FROM projects WHERE name=?',(project,))
    expected_cwd = str(registered['root']) if registered else ''
    for row in tx.query(
        "SELECT payload,path_norm FROM events WHERE kind='run' AND rc=0 AND project=? ORDER BY id DESC LIMIT 400",
        (project,),
    ):
        if _payload_tree(row["payload"]) == tree and any(
            _matches_acceptance_run(command, c, str(row["path_norm"] or ""), expected_cwd)
            for c in _payload_commands(row["payload"])
        ):
            return True
    return False


# Причина system-blocker, которую чинит платформа (оператор), а не проект. Всё остальное —
# проектная причина: 18.10 ревьюеры ставили system-blocker на красный живой тест проекта,
# на зависимость не в HEAD, на устаревший checker — и ревью ждало деплоя платформы, которого
# причина не требует. Сомнение решается в пользу платформы (прежнее поведение).
_PLATFORM_BLOCKER = re.compile(
    r"dev[-_]mcp|\bfleet[_ ]\w+|\bFLEET_[A-Z_]+|\bmcp\b|worker\.cmd|шлюз|gateway|прокси|proxy|"
    r"apply_patch|exec_preflight|страж|\bguard\b|policy\.toml|sandbox|песочниц|"
    r"инструмент\w*\s+(?:недоступ|не\s+отвеча|упал|завис)|tool\w*\s+(?:unavailable|timed?\s*out|missing)|"
    r"connector|коннектор|WinError\s*5\b|Access is denied|доступ\s+запрещ|запуск\s+(?:команды\s+)?запрещ",
    re.IGNORECASE,
)


BLOCKER_REQUEST_MARK = "Снять проектный блокер финального ревью"


def _down_world_resource(tx: Any, explanation: str) -> str | None:
    """A sensed world resource named in the note whose last sensor state is down."""
    from . import world
    cfg = world.config()
    text = explanation.casefold()
    for name, info in cfg.items():
        if (info.get("sensor") and name.casefold() in text
                and world.last_state(tx, name)[0] == world.DOWN):
            return name
    return None


def _busy_run_resource(explanation: str) -> str | None:
    """Ресурс уровня run («heavy-build»), о котором run ответил «сейчас занят»."""
    from . import world
    text = explanation.casefold()
    if "сейчас занят" not in text:
        return None
    for name in world.run_scoped():
        if f"«{name.casefold()}» сейчас занят" in text or f"{name.casefold()} сейчас занят" in text:
            return name
    return None


def is_platform_blocker(explanation: str) -> bool:
    return bool(_PLATFORM_BLOCKER.search(explanation or ""))


def _route_project_blocker(store: Any, worker_id: str, review_project: str, review_tz: str,
                           project: str, source_tz: str, explanation: str) -> str | None:
    """Проектный system-blocker: заявка в очередь проекта, ревью ждёт её accept, а не деплоя.

    Блок финального ревью уже записан (platform_deploy_after) — он остаётся запасным, если
    заявку открыть нельзя. Иначе поверх него пишется новый блок, связанный с заявкой.
    """
    if is_platform_blocker(explanation) or project != review_project:
        return None
    import hashlib

    # 20.10: a blocker request's own review hit the same red acceptance and opened the next
    # request, and so on — chains of 10+ submitted tasks behind frozen reviews (Beta App,
    # Mill Works). A second level is not a project fix: it goes to the operator's journal and
    # the review keeps its platform_deploy_after block.
    with store.read() as tx:
        source = tx.one("SELECT title FROM tasks WHERE project=? AND tz=?", (project, source_tz))
    if source is not None and BLOCKER_REQUEST_MARK in str(source["title"] or ""):
        from . import platform_manual
        fingerprint = hashlib.sha256(f"{project}/{source_tz}:blocker-loop".encode()).hexdigest()
        with store.tx() as tx:
            platform_manual.enqueue(
                tx, fingerprint, source_project=project, source_tz=review_tz,
                title=f"Петля блокеров: ревью заявки-снятия {project}/{source_tz} снова упёрлось в проект",
                detail=("Заявка, открытая снять проектный блокер, сама не проходит ревью по проектной "
                        "причине; новую заявку флот не открывает. Нужен оператор: приёмка задачи, "
                        "ТЗ или гейт проекта. Причина ревьюера: " + explanation)[:4000])
            tx.record_event(kind="blocker_loop_stopped", worker_id=worker_id, project=project,
                            tz=review_tz, payload={"source_tz": source_tz})
        return None

    ident = hashlib.sha256(f"{project}/{review_tz}:system-blocker".encode()).hexdigest()[:8]
    try:
        request_tz = open_request(
            store, ident, kind="integration", to=f"{project}/{source_tz}", project=project,
            worker_id=worker_id, source_tz=review_tz,
            summary=f"{BLOCKER_REQUEST_MARK} {project}/{source_tz}",
            detail=("Финальное ревью не смогло проверить работу по проектной причине; после "
                    "accept этой заявки ревью вернётся в очередь само. Причина ревьюера: "
                    + explanation)[:1800],
        )
    except (ValueError, OSError):
        return None
    if not request_tz:
        return None
    with store.tx() as tx:
        last = tx.one(
            "SELECT id,kind FROM events WHERE project=? AND tz=? AND kind IN "
            "('block','freeze','thaw','reactivate','approve','auto_reactivate') "
            "ORDER BY id DESC LIMIT 1", (review_project, review_tz))
        task = tx.one("SELECT state FROM tasks WHERE project=? AND tz=?", (review_project, review_tz))
        if last is None or last["kind"] != "block" or task is None or task["state"] != "frozen":
            return None
        tx.record_event(
            kind="block", worker_id=worker_id, project=review_project, tz=review_tz,
            payload={
                "reason": f"blocked: final review project blocker — ждёт {request_tz}: {explanation}"[:2000],
                "source_tz": source_tz, "automatic": True, "origin": "fleet_auto",
                "gate": "repair", "author_fault": False, "request": request_tz,
                "supersedes_block": int(last["id"]),
                "reactivate_policy": {"kind": "accepted_work",
                                      "target": f"{project}/{request_tz}",
                                      "authority": "fleet_task_events"},
            },
        )
    return request_tz


def decide(store: Any, worker_id: str, verdict: str, note: str, *,
           tree: str | None = None, claim_id: int | None = None,
           revision: str = "", action_receipt: dict[str, str] | None = None) -> None:
    """Вердикт ревью; действия, которым нужна своя транзакция, — после неё."""
    after: list[Any] = []
    _decide(store, worker_id, verdict, note, tree=tree, claim_id=claim_id,
            revision=revision, action_receipt=action_receipt, _after=after)
    for action in after:
        action()


def _decide(store: Any, worker_id: str, verdict: str, note: str, *,
            tree: str | None = None, claim_id: int | None = None,
            revision: str = "", action_receipt: dict[str, str] | None = None,
            _after: list[Any] | None = None) -> None:
    """Accept/reject/escalate the task addressed by the worker's active review claim.

    `tree` — хеш текущего состояния файлов проверяемой задачи (fleet.evidence);
    когда он известен, accept засчитывает только прогоны по этому состоянию.
    """
    verdict = (verdict or "").strip().casefold()
    if verdict not in {"accept", "reject", "escalate"}:
        raise ValueError("verdict must be 'accept', 'reject' or 'escalate'")
    if verdict == "escalate" and not (note or "").strip():
        raise ValueError("escalate требует note: что именно здесь проверить нельзя и почему")
    store.touch_worker(worker_id)

    with store.tx() as tx:
        review = tx.one(
            "SELECT c.id AS claim_id,c.started_at,t.* FROM claims c "
            "JOIN tasks t ON t.project=c.project AND t.tz=c.tz "
            "WHERE c.worker_id=? AND c.state='active' AND t.kind='review' "
            "ORDER BY c.id DESC LIMIT 1",
            (worker_id,),
        )
        if review is None:
            raise ValueError(f"{worker_id}: no active review task")
        if claim_id is not None and (
            type(claim_id) is not int or claim_id <= 0
            or int(review["claim_id"]) != claim_id
        ):
            raise ValueError("FLEET_STALE_CLAIM: claim_id does not match active review")
        project, source_tz = _review_target(
            str(review["tz"]), str(review["body"] or ""), str(review["project"])
        )
        if revision:
            if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
                raise ValueError("FLEET_STALE_CLAIM: invalid submitted revision")
            seal = tx.one(
                "SELECT payload FROM events WHERE project=? AND tz=? "
                "AND kind='claim_submission' ORDER BY id DESC LIMIT 1",
                (project, source_tz),
            )
            try:
                payload = json.loads(str(seal["payload"])) if seal else {}
            except (TypeError, ValueError):
                payload = {}
            if not isinstance(payload, dict) or payload.get("revision") != revision:
                raise ValueError("FLEET_STALE_CLAIM: submitted revision does not match review")
        original = tx.one("SELECT * FROM tasks WHERE project=? AND tz=?", (project, source_tz))
        if original is None:
            raise ValueError(f"{project}/{source_tz}: reviewed task not found")
        if original["state"] != "submitted":
            raise ValueError(f"{project}/{source_tz}: expected submitted task")

        acceptance = _json_list(original["acceptance"])
        gate_cmds = _gate_commands(project, acceptance)

        def unproven(commands: list[str]) -> list[str]:
            return [c for c in commands if not (c in gate_cmds and _shared_proof(tx, project, c, tree))]

        if verdict == "accept":
            if not _blind_submission_sealed(tx, original):
                raise ValueError(
                    "Mill Works blind review: server-sealed independent attempt "
                    "is required; a green review-check alone is not proof"
                )
            # План консорциума 11.10, п.3: ревью судит снимок кандидата; цвет ствола держит
            # интеграцию, а не вердикт. Иначе 11.10 сложился круг ревью ↔ ствол ↔ стыки (урок 34).
            red: list[str] = []
            if red:
                raise ValueError(
                    f"accept ждёт стыка: {red}. Принять правку в сломанное место — спрятать, что "
                    "сломало ствол. Верни ревью через fleet_finish — оно вернётся в очередь и будет "
                    "выдано, когда гейт позеленеет. reject по существу правки — можно и сейчас."
                )
            since = str(review["started_at"])
            missing = unproven(_missing_runs(tx, worker_id, since, acceptance))
            if missing:
                raise ValueError(
                    "accept requires reviewer's own successful run events for: "
                    + "; ".join(missing)
                )
            stale = unproven(_missing_runs(tx, worker_id, since, acceptance, tree)) if tree else []
            if stale:
                raise ValueError(
                    "успешные прогоны есть, но файлы задачи менялись после них — они "
                    "доказывают уже не то состояние, которое принимается. Не правь файлы "
                    "во время ревью; прогони заново: " + "; ".join(stale)
                )

        is_final_review = FINAL_MARK in str(review["body"] or "")
        if verdict == "escalate" and is_final_review:
            # A broken reviewer runner / unavailable platform facility is not a
            # defect in the author's work. Do not force a false reject, accept
            # without proof, or generate an endless chain of final reviews.
            explanation = str(note or "").strip()
            prefix = "system-blocker:"
            if (not explanation.casefold().startswith(prefix)
                    or len(explanation[len(prefix):].strip()) < 20):
                raise ValueError(
                    "Финальное ревью: повторный обычный escalate недоступен. "
                    "Если заблокирована сама проверочная инфраструктура, укажи "
                    "note='system-blocker: <точная команда, ошибка, шаги воспроизведения>'; "
                    "это отложит ревью, не отклоняя работу автора. Иначе accept с "
                    "доказанной приёмкой или reject с подтверждённым дефектом."
                )
            now = _now()
            # A sensed world resource that is down right now (build-disk: C: below the
            # packaging reserve) and named in the note is the cause; its sensor coming
            # back up is the authority. 20.10.2026: 14 Beta App reviews froze on a
            # full disk and waited for a platform deploy that nothing would trigger.
            busy = _busy_run_resource(explanation)
            if busy:
                # 21.10.2026: ревью Beta App заморозилось «system-blocker» на «ресурс
                # heavy-build сейчас занят» — временной занятости мьютекса check_all; на нём
                # выросла цепочка из шести заявок-блокеров. Занятость — не блокер: задача
                # возвращается в очередь без заморозки и без заявки проекту.
                tx.changes(
                    [
                        ("UPDATE tasks SET state='ready',updated_at=? WHERE project=? AND tz=?",
                         (now, review["project"], review["tz"])),
                        ("UPDATE claims SET state='released',released_at=? WHERE id=?",
                         (now, review["claim_id"])),
                    ],
                    event={
                        "kind": "resource_busy_requeue", "worker_id": worker_id,
                        "project": str(review["project"]), "tz": str(review["tz"]),
                        "payload": {"resource": busy, "source_tz": source_tz,
                                    "reason": explanation[:500]},
                    },
                )
                return
            world_down = _down_world_resource(tx, explanation)
            policy = ({"kind": "world_resource_up", "target": world_down,
                       "authority": "world_sensor"} if world_down else
                      {"kind": "platform_deploy_after", "target": now,
                       "authority": "platform_guard"})
            tx.changes(
                [
                    (
                        "UPDATE tasks SET state='frozen',updated_at=? WHERE project=? AND tz=?",
                        (now, review["project"], review["tz"]),
                    ),
                    (
                        "UPDATE claims SET state='released',released_at=? WHERE id=?",
                        (now, review["claim_id"]),
                    ),
                ],
                event={
                    "kind": "block", "worker_id": worker_id,
                    "project": str(review["project"]), "tz": str(review["tz"]),
                    "payload": {
                        "reason": "blocked: final review infrastructure — " + explanation,
                        "source_tz": source_tz, "automatic": True,
                        "origin": "fleet_auto", "gate": "operational",
                        "author_fault": False,
                        # An external trusted receipt (a deployment, or the server's
                        # own world sensor), not the red acceptance, a free-form note
                        # or a retry timer, is the only automatic authority to revisit.
                        "reactivate_policy": policy,
                    },
                },
            )
            if _after is not None and not world_down:
                # A down world resource is not project work: no request an agent could do.
                review_project, review_tz = str(review["project"]), str(review["tz"])
                _after.append(lambda: _route_project_blocker(
                    store, worker_id, review_project, review_tz, project, source_tz, explanation))
            return
        # Финальный круг исправлений: при зелёной приёмке ревьюера reject не рождает ещё один
        # fix — работа принимается, а замечание становится отдельной задачей (follow-up).
        follow_up = ""
        if (verdict == "reject" and original["kind"] == "fix"
                and FINAL_MARK in str(original["body"] or "")
                and not (project == "Mill Works" and re.fullmatch(
                    r"fix:TZ-MR-batch-[a-z0-9]{6,40}(?::[1-9][0-9]*)?",
                    str(original["tz"])))):
            since = str(review["started_at"])
            green = bool(acceptance) and not unproven(_missing_runs(tx, worker_id, since, acceptance)) and not (
                tree and unproven(_missing_runs(tx, worker_id, since, acceptance, tree))
            )
            if green:  # цвет ствола вердикт не держит (п.3 плана консорциума)
                verdict, follow_up = "accept", (note or "").strip() or "(замечание не уточнено)"

        now = _now()
        if verdict == "escalate":
            # Проверить здесь нельзя: ревью закрывается и сразу открывается финальное ревью
            # для ДРУГОГО агента (queue._reviewed_before), без права на escalate. До 25.09.2026
            # ревью уходило владельцу в draft — и семья задач стояла, пока он не придёт.
            tx.changes(
                [
                    (
                        "UPDATE tasks SET state='accepted',updated_at=? WHERE project=? AND tz=?",
                        (now, review["project"], review["tz"]),
                    ),
                    (
                        "UPDATE claims SET state='released',released_at=? WHERE id=?",
                        (now, review["claim_id"]),
                    ),
                ],
                event={
                    "kind": "escalate",
                    "worker_id": worker_id,
                    "project": project,
                    "tz": str(review["tz"]),
                    "payload": {"note": note, "review_of": source_tz},
                },
            )
            _open_review_tx(tx, project, source_tz, final_note=note)
            from .actions import record_receipt
            record_receipt(tx, worker_id, int(review["claim_id"]), action_receipt)
            return
        fix_source = _fix_source(str(original["body"] or ""), project) if original["kind"] == "fix" else None
        if original["kind"] == "fix" and project == "Mill Works":
            # A blind MR fix is delivered with a server-owned case-only body.
            # Its original [fleet-fix] header has deliberately been removed;
            # derive the parent from the *typed task id*, not rejection prose.
            blind_fix = re.fullmatch(
                r"fix:(TZ-MR-batch-[0-9a-z]{6,40})(?::[1-9][0-9]*)?",
                str(original["tz"]),
            )
            if blind_fix is not None:
                fix_source = (project, blind_fix.group(1))
        root_project, root_tz = fix_source or (project, source_tz)
        root = (
            tx.one("SELECT * FROM tasks WHERE project=? AND tz=?", (root_project, root_tz))
            if fix_source is not None
            else original
        )
        if root is None:
            raise ValueError(f"{root_project}/{root_tz}: fix source task not found")

        statements: list[tuple[str, tuple[Any, ...]]] = [
            (
                "UPDATE tasks SET state='accepted',updated_at=? WHERE project=? AND tz=?",
                (now, review["project"], review["tz"]),
            ),
            (
                "UPDATE claims SET state='released',released_at=? WHERE id=?",
                (now, review["claim_id"]),
            ),
            (
                "UPDATE claims SET state='released',released_at=? "
                "WHERE project=? AND tz=? AND state='active'",
                (now, project, source_tz),
            ),
        ]
        fix_tz: str | None = None
        follow_tz: str | None = None
        final_round, hard_round, rounds = False, False, 0
        if verdict == "accept":
            statements.append(
                (
                    "UPDATE tasks SET state='accepted',updated_at=? WHERE project=? AND tz=?",
                    (now, project, source_tz),
                )
            )
            if fix_source is not None:
                statements.append(
                    (
                        "UPDATE tasks SET state='accepted',updated_at=? WHERE project=? AND tz=?",
                        (now, root_project, root_tz),
                    )
                )
            if follow_up:
                follow_tz = _next_tz(tx, root_project, f"request:{uuid.uuid4().hex[:8]}")
                follow_body = "\n".join([
                    f"[fleet-request] id={follow_tz.split(':', 1)[1]}",
                    "Кому: queue",
                    "Тип: follow-up финального круга",
                    f"Что нужно: {root_tz} принята на финальном круге исправлений при зелёной приёмке; "
                    f"довести замечание ревьюера {worker_id}: {follow_up[:1500]}",
                    "Правь только то, что относится к замечанию; приёмка — как у исходной задачи.",
                ])
                statements.append(
                    (
                        "INSERT INTO tasks("
                        "project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,"
                        "resources,acceptance,author_worker,body,created_at,updated_at"
                        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            root_project,
                            follow_tz,
                            f"Follow-up {root_tz}: замечание финального ревью",
                            "ready",
                            int(root["priority"]),
                            "request",
                            str(root["spec_path"] or ""),
                            str(root["spec_sha"] or ""),
                            "[]",
                            str(root["owns"] or "[]"),
                            str(root["resources"] or "[]"),
                            str(root["acceptance"] or "[]"),
                            None,
                            follow_body,
                            now,
                            now,
                        ),
                    )
                )
        else:
            if fix_source is not None:
                statements.append(
                    (
                        "UPDATE tasks SET state='rejected',updated_at=? WHERE project=? AND tz=?",
                        (now, project, source_tz),
                    )
                )
            # A rejected source stays submitted while its repair is queued. This
            # prevents the source and the repair from becoming two parallel jobs
            # with the same ownership.
            fix_tz = _next_tz(tx, root_project, f"fix:{root_tz}")
            # Предохранитель от порочного круга: исправления раз за разом упираются в новые
            # замечания — обычно это ТЗ без границ. С MAX_FIX_ROUNDS fix идёт финальным кругом
            # (его ревью при зелёной приёмке примет работу), а после MAX_FIX_ROUNDS_HARD
            # следующий fix становится крупным diagnostic/end-to-end repair вместо freeze.
            rounds = int(
                tx.scalar(
                    "SELECT COUNT(*) FROM tasks WHERE project=? AND kind='fix' AND (tz=? OR tz LIKE ?)",
                    (root_project, f"fix:{root_tz}", f"fix:{root_tz}:%"),
                )
                or 0
            )
            final_round = rounds >= MAX_FIX_ROUNDS
            hard_round = rounds >= MAX_FIX_ROUNDS_HARD
            body_lines = [
                f"[fleet-fix] source={root_project}/{root_tz}",
                f"Вердикт ревьюера {worker_id}: reject.",
                f"Что исправить: {(note or '').strip() or '(причина не уточнена)'}",
                "После исправления заново прогони acceptance исходной задачи.",
            ]
            if final_round:
                body_lines.append(f"Это {rounds + 1}-й отказ подряд. " + FINAL_FIX_NOTE)
            if hard_round:
                body_lines.append(HARD_FIX_NOTE)
            body = "\n".join(body_lines)
            statements.append(
                (
                    "INSERT INTO tasks("
                    "project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,"
                    "resources,acceptance,author_worker,body,created_at,updated_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        root_project,
                        fix_tz,
                        f"Fix {root_tz}",
                        "ready",
                        int(root["priority"]),
                        "fix",
                        str(root["spec_path"] or ""),
                        str(root["spec_sha"] or ""),
                        "[]",
                        str(root["owns"] or "[]"),
                        str(root["resources"] or "[]"),
                        str(root["acceptance"] or "[]"),
                        None,
                        body,
                        now,
                        now,
                    ),
                )
            )
        tx.changes(
            statements,
            event={
                "kind": verdict,
                "worker_id": worker_id,
                "project": project,
                "tz": source_tz,
                "payload": {
                    "note": note,
                    "review_task": str(review["tz"]),
                    "fix_task": fix_tz,
                    "follow_up_task": follow_tz,
                    "converted_from_reject": bool(follow_up),
                    "fix_round": rounds + 1 if fix_tz is not None else 0,
                    "diagnostic_repair": bool(fix_tz is not None and hard_round),
                },
            },
        )
        from .actions import record_receipt
        record_receipt(tx, worker_id, int(review["claim_id"]), action_receipt)


def _request_destination(tx: Any, to: str, project: str | None) -> tuple[str, str]:
    if to.casefold() == "queue":
        if project:
            if tx.one("SELECT name FROM projects WHERE name=? AND active=1", (project,)) is None:
                raise ValueError(f"project not found or inactive: {project}")
            return project, "queue"
        projects = tx.query("SELECT name FROM projects WHERE active=1 ORDER BY priority,name")
        if len(projects) != 1:
            raise ValueError("to='queue' needs project when multiple projects are active")
        return str(projects[0]["name"]), "queue"

    if "/" not in to:
        raise ValueError("to must be 'owner', 'queue', or '<project>/<tz>'")
    target_project, target_tz = to.rsplit("/", 1)
    if not target_project or not target_tz:
        raise ValueError("to must be '<project>/<tz>'")
    if tx.one("SELECT name FROM projects WHERE name=? AND active=1", (target_project,)) is None:
        raise ValueError(f"project not found or inactive: {target_project}")
    return target_project, target_tz


def open_request(
    store: Any,
    request_id: str,
    *,
    kind: str,
    summary: str,
    detail: str = "",
    to: str = "owner",
    project: str | None = None,
    worker_id: str | None = None,
    source_tz: str | None = None,
    path: str = "",
    lists: Iterable[str] | None = None,
) -> str | None:
    """Route a non-access request into fleet as a ready task (с 25.09.2026 — без одобрения владельца).

    None means the caller must keep the legacy owner/access request path.

    TZ-04 integration signature:
      request_note(kind, summary, detail='', path='', lists=None,
                   to='owner', agent='')
    """
    kind = (kind or "other").strip().casefold()
    to = (to or "owner").strip()
    if kind in _ACCESS_KINDS or to.casefold() == "owner":
        return None
    if not request_id.strip() or not summary.strip():
        raise ValueError("request_id and summary are required")
    lists = tuple(lists or ())

    # TZ-314: a foreign-project defect in platform code belongs to the platform
    # lane. Park its source and return the single deduplicated platform request
    # instead of creating another request in the current project's queue.
    if kind == "other" and project and source_tz:
        from . import platform_route

        platform_route.require_request_allowed(store, project, source_tz)
        routed = platform_route.route_request(
            store, source_project=project, source_tz=source_tz,
            summary=summary, detail=detail, path=path, worker_id=worker_id,
        )
        if routed.status == "rejected":
            raise ValueError(
                routed.detail or f"повторная платформенная заявка: {routed.task}"
            )
        if routed.status in ("created", "linked") and routed.task is not None:
            return routed.task.rsplit("/", 1)[1]

    with store.tx() as tx:
        task_project, addressed_to = _request_destination(tx, to, project)
        target = None
        if addressed_to != "queue":
            target = tx.one(
                "SELECT * FROM tasks WHERE project=? AND tz=?",
                (task_project, addressed_to),
            )
            if target is None and (re.fullmatch(r"TZ-[\w-]+", addressed_to, re.I)
                                   or addressed_to.startswith(("fix:", "review:", "request:", "triage:"))):
                raise ValueError(
                    f"задача {task_project}/{addressed_to} не зарегистрирована; "
                    "её owns/resources/acceptance нельзя наследовать. "
                    "Сначала зарегистрируй исходное ТЗ через bootstrap; "
                    "для самостоятельной заявки укажи to='queue' и точный path."
                )

        owns: tuple[str, ...] = ()
        resources: tuple[str, ...] = ()
        acceptance: tuple[str, ...] = ()
        if target is not None:
            owns = _json_list(target["owns"])
            resources = _json_list(target["resources"])
            acceptance = _json_list(target["acceptance"])

        if path.strip():
            root_row = tx.one("SELECT root FROM projects WHERE name=?", (task_project,))
            if root_row is None:
                raise ValueError(f"project root not found: {task_project}")
            requested_path = norm_path(path.strip(), project_root=str(root_row["root"]))
            if owns and not any(paths_overlap(requested_path, owned) for owned in owns):
                raise ValueError(
                    f"request path {path.strip()!r} is outside target ownership "
                    f"{task_project}/{addressed_to}"
                )
            owns = (requested_path,)

        # Заявка сразу становится работой, поэтому без зоны она невыполнима: вкладке с пустыми
        # owns страж не даст записать ни одного файла. Лучше отказ сразу — с рецептом агенту.
        if not owns:
            raise ValueError(
                "заявке нужна зона: передай path (файл или папка, которую надо поправить) или "
                "to='<проект>/<tz>' задачи, чьи owns покрывают правку. Решение внутри своего ТЗ — "
                "принимай сам; внешнее препятствие — fleet_block(agent, reason)."
            )
        # Request identity survives a different caller, claim or wording of the
        # path. The immutable event, not mutable author_worker, owns this key.
        import hashlib
        request_key = hashlib.sha256(json.dumps({
            'kind': kind, 'target': addressed_to, 'summary': summary.strip(),
            'detail': detail.strip(), 'owns': sorted(owns),
            'lists': sorted(str(x).strip() for x in (lists or ()) if str(x).strip()),
        }, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
        duplicate = tx.one(
            "SELECT t.tz FROM events e JOIN tasks t ON t.project=e.project AND t.tz=e.tz "
            "WHERE e.kind='request_draft' AND e.project=? "
            "AND t.state IN ('draft','ready','taken','submitted','frozen') "
            "AND json_extract(e.payload,'$.request_key')=? ORDER BY e.id DESC LIMIT 1",
            (task_project, request_key))
        if duplicate:
            return str(duplicate['tz'])
        # Legacy requests have no structured key. Preserve their old fallback.
        for row in tx.query(
            "SELECT tz,body FROM tasks WHERE project=? AND kind='request' "
            "AND state IN ('draft','ready','taken','submitted') AND author_worker IS ? "
            "AND NOT EXISTS (SELECT 1 FROM events e WHERE e.project=tasks.project AND e.tz=tasks.tz "
            "AND e.kind='request_draft' AND json_extract(e.payload,'$.request_key') IS NOT NULL)",
            (task_project, worker_id),
        ):
            if f"Что нужно: {summary.strip()}" in str(row["body"] or ""):
                return str(row["tz"])

        task_tz = _next_tz(tx, task_project, f"request:{request_id.strip()}")
        now = _now()
        lines = [
            f"[fleet-request] id={request_id.strip()}",
            f"Кому: {to}",
            f"Тип: {kind}",
            f"Что нужно: {summary.strip()}",
        ]
        if detail.strip():
            lines.append(f"Зачем / детали: {detail.strip()}")
        source = ", ".join(
            bit for bit in (
                f"worker={worker_id}" if worker_id else "",
                f"tz={source_tz}" if source_tz else "",
            ) if bit
        )
        if source:
            lines.append(f"Источник: {source}")
        if path.strip():
            lines.append(f"Путь: {path.strip()}")
        cleaned = [str(x).strip() for x in (lists or ()) if str(x).strip()]
        if cleaned:
            lines.append("Списки: " + ", ".join(cleaned))
        lines.append(
            "Заявка на правку в зоне этой задачи: сделай то, что просит «Что нужно», не шире; "
            "приёмка — как указано в памятке. Просьба бессмысленна или уже выполнена — запиши это "
            "в note сдачи (fleet_submit): ревьюер подтвердит."
        )

        tx.change(
            "INSERT INTO tasks("
            "project,tz,title,state,priority,kind,spec_path,spec_sha,needs,owns,"
            "resources,acceptance,author_worker,body,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_project,
                task_tz,
                f"Request: {summary.strip()[:120]}",
                "ready",
                int(target["priority"]) if target is not None else 5,
                "request",
                str(target["spec_path"] or "") if target is not None else "",
                "",
                "[]",
                json.dumps(list(owns), ensure_ascii=False),
                json.dumps(list(resources), ensure_ascii=False),
                json.dumps(list(acceptance), ensure_ascii=False),
                worker_id,
                "\n".join(lines),
                now,
                now,
            ),
            event={
                "kind": "request_draft",
                "worker_id": worker_id,
                "project": task_project,
                "tz": task_tz,
                "payload": {
                    "request_id": request_id.strip(),
                    "to": to,
                    "addressed_to": addressed_to,
                    "source_tz": source_tz,
                    "request_key": request_key,
                },
            },
        )
        return task_tz


__all__ = ["decide", "open_request", "open_review"]
