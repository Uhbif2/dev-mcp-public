"""Надзор рабочего хода: локальная Qwen3-4B, заземление и молчание по умолчанию.

`fleet_overseer_observe` получает снимок законченного хода в shadow/send-режиме.
Текущая семантическая ветка сопоставляет ответ с содержательными целями владельца,
генерирует сообщение только через бесплатный локальный мост 127.0.0.1:11435,
проверяет цитаты, уверенность и повторы; при отсутствии доказательств молчит.
История действий берётся по тегу этого чата, пробы — из проекта, не из общего diff.
Для каждого хода пишется очищенный архив `overseer/replies/<chat>.jsonl` для реплея.
Legacy decision-model функции ниже сохранены для совместимости.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import threading
import time
import tomllib
import urllib.request
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from dev_mcp.json_book import read_json, write_json
from typing import Any, Callable

MARK = "⟦надзор⟧"
RELAY_URL = os.environ.get("DEV_MCP_DECISIONS_URL", "http://127.0.0.1:8790/v1beta/decisions")
MODEL = "gemini-decision-1"
NO_DECISION_BACKEND = "disabled: no decision backend"
NO_REDACTOR = "disabled: redaction unavailable"
ROOT = Path(__file__).resolve().parents[3]
_lock = threading.Lock()
MAX_REPLY_ARCHIVE_BYTES = 20 * 1024 * 1024

# Archive sanitization must work even if Mill Works's optional redactor is unavailable.
_ARCHIVE_PATTERNS = (
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+={0,2}"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)\b(?:sk[-_]|gh[pousr]_|github_pat_|glpat-|xox[abprs]-)[A-Za-z0-9_-]{6,}\b"),
     "[REDACTED]"),
    (re.compile(r"(?i)(\b(?:api[_-]?key|access[_-]?token|password|secret|token)\s*[=:]\s*[\"']?)[^\s\"'&;,]+"),
     r"\1[REDACTED]"),
    (re.compile(r"(?<![\w])(?:[A-Fa-f0-9]{40,})(?![\w])"), "[REDACTED]"),
    (re.compile(r"(?<![\w])(?:[A-Za-z0-9+/]{64,}={0,2}|[A-Za-z0-9_-]{80,})(?![\w])"),
     "[REDACTED]"),
)


def _archive_redact(value: str) -> str:
    result = str(value)
    for pattern, replacement in _ARCHIVE_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def _recent_owner_replies(messages: list[str], limit: int = 2000) -> list[str]:
    """Preserve the last owner messages (aggregate character cap), not the first 2K."""
    result: list[str] = []
    remaining = limit
    for message in reversed(messages):
        if remaining <= 0:
            break
        safe = _archive_redact(message)
        item = safe[-remaining:]
        if item:
            result.append(item)
            remaining -= len(item)
    return list(reversed(result))


@lru_cache(maxsize=1)
def _redactor() -> Callable[[str], tuple[str, dict[str, int]]]:
    """Load the shared deterministic redactor without changing sys.path."""
    path = ROOT.parent / "mill-works" / "mill" / "redact.py"
    if not path.is_file():
        raise ImportError("redactor is missing")
    spec = importlib.util.spec_from_file_location("_overseer_redact", path)
    if spec is None or spec.loader is None:
        raise ImportError("redactor is missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.redact


def _redact_text(value: str) -> str:
    """Use Mill Works's redactor; never return unfiltered data on error."""
    try:
        return _redactor()(value)[0]
    except Exception:  # noqa: BLE001 - fail closed on missing or broken sanitizer
        return "[REDACTION_UNAVAILABLE]"


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {(_redact_text(k) if isinstance(k, str) else k): _scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


def _safe_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Текст — через redact; картинки — только снимки проб, которые владелец сам объявил в
    `[[projects.X.probe]] image` (экран своего приложения/телефона — главная улика GUI, план 59)."""
    return [_scrub(b) if b.get("type") == "text" else {k: v for k, v in b.items() if k != "probe_image"}
            for b in blocks
            if b.get("type") == "text" or (b.get("type") == "image" and b.get("probe_image"))]

KINDS = {
    "requirement": "требование: результат или изменение, которое должно появиться",
    "criterion": "критерий: как проверить, что значит «готово»",
    "constraint": "ограничение: как делать или чего не делать",
    "decision": "решение владельца, уже принятое в тексте (ответ на возможный вопрос)",
    "question": "вопрос владельца, на который нужен ответ или исследование",
    "context": "контекст, сведения, причины",
    "noise": "приветствие, служебное, шум",
}
PROOFS = {
    "code": "виден по изменённым файлам и коду",
    "probe": "проверяется командой: тесты, сборка, запуск CLI",
    "visual": "проверяется глазами: экран приложения на ПК или телефоне, анимация, вид",
    "live": "нужен реальный внешний мир: платёж, вход в аккаунт, другой компьютер",
    "owner": "сделать или решить может только владелец",
    "answer": "закрывается ответом в чате (вопрос, исследование)",
}
WORK_KINDS = {"requirement", "criterion", "question"}
DONE_MIN = 0.8          # ниже — не закрыто (дешёвая ошибка: лишний круг)
CLAIM_MIN = 0.7
FLAG_MIN = 0.7
STALL_TURNS = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def chat_id(url: str) -> str:
    m = re.search(r"chatgpt\.com/(?:g/[^/]+/)?c/([0-9a-fA-F-]{8,})", str(url or ""))
    return m.group(1).lower() if m else ""


def tag_of(cid: str) -> str:
    return "S-" + cid.replace("-", "")[:10]


# ---------------------------------------------------------------- хранение
class Book:
    def __init__(self, data_dir: Path) -> None:
        self.dir = Path(data_dir) / "overseer"
        self.registry = self.dir / "chats.json"
        self.journal = self.dir / "journal.jsonl"

    def _write(self, path: Path, value: Any) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        write_json(path, value, indent=1)

    def chats(self) -> dict[str, dict[str, Any]]:
        return read_json(self.registry, default={}, strict=True)

    def save_chats(self, chats: dict[str, Any]) -> None:
        self._write(self.registry, chats)

    def card(self, cid: str) -> dict[str, Any] | None:
        return read_json(self.dir / f"{cid}.json")

    def save_card(self, card: dict[str, Any]) -> None:
        self._write(self.dir / f"{card['chat']}.json", _scrub(card))

    def log(self, row: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.journal.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(_scrub({"ts": _now(), **row}), ensure_ascii=False) + "\n")

    def reply_archive(self, cid: str) -> Path:
        if not re.fullmatch(r"[0-9a-f-]{8,64}", cid):
            raise ValueError("invalid chat id")
        return self.dir / "replies" / f"{cid}.jsonl"

    def archive_turn(self, cid: str, row: dict[str, Any]) -> None:
        """Append one sanitized turn; trim oldest complete JSONL rows at 20 MiB."""
        path = self.reply_archive(cid)
        path.parent.mkdir(parents=True, exist_ok=True)
        safe = {k: ([_archive_redact(str(v)) for v in value] if isinstance(value, list)
                    and k == "owner_replies" else
                    _archive_redact(value) if isinstance(value, str) else value)
                for k, value in row.items()}
        # Enforce field limits at the storage boundary, not only in observe().
        safe["reply"] = str(safe.get("reply") or "")[-6000:]
        safe["owner_replies"] = _recent_owner_replies(safe.get("owner_replies") or [])
        # Actions/probes are optionally stored for a faithful, offline replay.
        for key in ("agent_actions", "probes"):
            if key in safe:
                safe[key] = json.loads(_archive_redact(json.dumps(safe[key], ensure_ascii=False)))
        payload = (json.dumps({"ts": _now(), "chat": cid, **safe}, ensure_ascii=False) + "\n").encode("utf-8")
        with path.open("ab") as stream:
            stream.write(payload)
        if path.stat().st_size > MAX_REPLY_ARCHIVE_BYTES:
            data = path.read_bytes()
            # Discard only whole oldest rows; never truncate UTF-8/JSON halfway.
            boundary = data.find(b"\n", len(data) - MAX_REPLY_ARCHIVE_BYTES)
            kept = data[boundary + 1:] if boundary >= 0 else payload
            replacement = path.with_suffix(".jsonl.tmp")
            replacement.write_bytes(kept)
            os.replace(replacement, path)


def stats_24h(book: Book, cid: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Per-chat 24-hour messages, silences by cause, and owner replies in journal."""
    moment = now or datetime.now(timezone.utc)
    since = moment.timestamp() - 86400
    result: dict[str, Any] = {"turns": 0, "messages": 0, "silences": {}, "owner_reactions": 0}
    try:
        with book.reply_archive(cid).open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    stamp = datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00"))
                    if stamp.timestamp() < since:
                        continue
                except (ValueError, KeyError, TypeError):
                    continue
                result["turns"] += 1
                reason = str(row.get("skipped_reason") or "")
                if not reason and row.get("mode") == "send" and row.get("sent_text"):
                    result["messages"] += 1
                else:
                    reason = reason or ("shadow" if row.get("mode") != "send" else "no_message")
                    result["silences"][reason] = result["silences"].get(reason, 0) + 1
    except OSError:
        pass
    try:
        with book.journal.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    if row.get("chat") != cid or row.get("event") != "owner_reply":
                        continue
                    stamp = datetime.fromisoformat(str(row["ts"]).replace("Z", "+00:00"))
                    if stamp.timestamp() >= since and row.get("after_verdict"):
                        result["owner_reactions"] += 1
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        pass
    return result


def format_24h_stats(book: Book, cid: str) -> str:
    """Ready-to-append console metric; tools/fleet.py remains outside this task's write scope."""
    data = stats_24h(book, cid)
    reasons = ", ".join(f"{reason}={count}" for reason, count in sorted(data["silences"].items())) or "нет"
    return (f"24ч: ходов {data['turns']}, сообщений {data['messages']}, "
            f"молчаний [{reasons}], реакций владельца {data['owner_reactions']}")


ENGINES = ("proxy", "semantic")  # proxy — надзор «за владельца» (overseer_proxy.py, 21.11)


def add(book: Book, url: str, project: str, mode: str = "shadow",
        engine: str | None = None) -> dict[str, Any]:
    cid = chat_id(url)
    if not cid:
        raise ValueError("нужен адрес чата вида https://chatgpt.com/c/<id>")
    if mode not in ("shadow", "send", "off"):
        raise ValueError("режим: shadow | send | off")
    if engine is not None and engine not in ENGINES:
        raise ValueError("движок: proxy | semantic")
    with _lock:
        chats = book.chats()
        entry = {**chats.get(cid, {}), "url": f"https://chatgpt.com/c/{cid}", "project": project,
                 "mode": mode, "tag": tag_of(cid)}
        if engine:
            entry["engine"] = engine
        entry.pop("status", None)  # explicit re-mark permits recovery
        entry.setdefault("added", _now())
        chats[cid] = entry
        book.save_chats(chats)
    return {"chat": cid, **entry}


def set_mode(book: Book, cid_or_url: str, mode: str, engine: str | None = None) -> dict[str, Any]:
    cid = chat_id(cid_or_url) or cid_or_url
    with _lock:
        chats = book.chats()
        if cid not in chats:
            raise ValueError(f"чат {cid} не под надзором")
        if mode not in ("shadow", "send", "off"):
            raise ValueError("режим: shadow | send | off")
        if engine is not None and engine not in ENGINES:
            raise ValueError("движок: proxy | semantic")
        chats[cid]["mode"] = mode
        chats[cid].pop("status", None)  # explicit re-mark permits recovery
        if engine:
            chats[cid]["engine"] = engine
        book.save_chats(chats)
    return {"chat": cid, **chats[cid]}


def _set_backend_status(book: Book, cid: str, status: str) -> None:
    """Registry contains only control metadata; caller holds _lock.

    Do not use the redacted card/journal when the redactor itself is missing.
    Persist a transition once, without any chat content or owner notice.
    """
    chats = book.chats()
    entry = chats.get(cid)
    if entry is None or entry.get("status", "") == status:
        return
    if status:
        entry["status"] = status
    else:
        entry.pop("status", None)
    book.save_chats(chats)


def watched(book: Book) -> list[dict[str, Any]]:
    return [{"chat": k, "url": v["url"], "mode": v["mode"], "engine": v.get("engine") or "semantic",
             **({"status": v["status"]} if v.get("status") else {})}
            for k, v in book.chats().items() if v.get("mode") in ("shadow", "send")]


# ---------------------------------------------------------------- карточка
_NUM = re.compile(r"^\s*(\d{1,3})[.)]\s+(.+)$")


def _non_task(text: str) -> bool:
    """A push to continue, or pasted console output, is not a new deliverable.

    Keep these strings in sources for the owner audit, but exclude them from
    requirements even when an older card has cached a model's wrong kind.
    Do not discard an actual correction like 'Продолжи — только ...'.
    """
    value = " ".join(str(text).casefold().split()).strip()
    if re.fullmatch(r"(?:продолжи|продолжай|продолжайте)[.!…]*", value):
        return True
    if re.fullmatch(r"(?:запустил|роль я выдал)\s*[—–:-]\s*продолжи[.!…]*", value):
        return True
    return value.startswith(("ps c:\\", ">> "))


def _work_items(card: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in card["segments"]
            if s["status"] == "open" and s["kind"] in WORK_KINDS and not _non_task(s["text"])]


def _next_items(card: dict[str, Any]) -> list[dict[str, Any]]:
    """A numbered checklist has a heading, but later owner corrections still count."""
    work = _work_items(card)
    if (len(work) > 1 and any(_NUM.match(s["text"]) for s in work)
            and work[0]["origin"] == "first" and not _NUM.match(work[0]["text"])):
        return work[1:]
    return work


def _goal(card: dict[str, Any]) -> str:
    """The first substantial owner request is the durable goal, not the last nudge."""
    for s in card["segments"]:
        if s["origin"] == "first" and not _non_task(s["text"]):
            return s["text"]
    for s in card["segments"]:
        if not _non_task(s["text"]):
            return s["text"]
    return ""


def _focus_quote(text: str, n: int = 180) -> str:
    """Quote the owner's own command rather than attachment/console chrome."""
    clean = " ".join(str(text).split())
    clean = re.sub(r"^\S+\.(?:jsonl?|JSONL?)\s*(?:Code|Document)\s*", "", clean)
    clean = re.sub(r"^Dev's Computer dev-mcp\+\s*", "", clean)
    return _quote(clean, n)


def segment(text: str) -> list[str]:
    """Дословные куски: пункты владельца по его нумерации, иначе абзацы, длинные — по фразам."""
    lines = [l.rstrip() for l in str(text or "").splitlines()]
    numbered = [i for i, l in enumerate(lines) if _NUM.match(l)]
    out: list[str] = []
    if len(numbered) >= 2:
        head = "\n".join(lines[:numbered[0]]).strip()
        if head:
            out.append(head)
        for n, start in enumerate(numbered):
            end = numbered[n + 1] if n + 1 < len(numbered) else len(lines)
            out.append("\n".join(lines[start:end]).strip())
    else:
        for para in re.split(r"\n\s*\n", str(text or "")):
            para = para.strip()
            if not para:
                continue
            if len(para) <= 400:
                out.append(para)
            else:
                out.extend(s.strip() for s in re.split(r"(?<=[.!?…])\s+", para) if s.strip())
    return [s for s in out if s][:150]


def _add_source(card: dict[str, Any], text: str, origin: str) -> list[str]:
    ids = []
    for piece in segment(text):
        sid = f"s{len(card['segments']) + 1}"
        card["segments"].append({"id": sid, "text": piece, "origin": origin,
                                 "kind": "context" if _non_task(piece) else "",
                                 "proof": "", "status": "open"})
        ids.append(sid)
    card["sources"].append({"sha": _sha(text), "origin": origin, "at": _now()})
    return ids


def new_card(cid: str, entry: dict[str, Any], first: str) -> dict[str, Any]:
    card = {"chat": cid, "url": entry["url"], "project": entry.get("project", ""),
            "tag": entry.get("tag") or tag_of(cid), "created": _now(), "segments": [],
            "sources": [], "sent": [], "turns": 0, "last_turn": "", "last_turn_at": "",
            "idle_turns": 0, "repeat_turns": 0, "probe_errors": {}, "protocol_sent": False,
            "last_verdict": ""}
    _add_source(card, first, "first")
    return card


# ---------------------------------------------------------------- улики
def agent_history(tag: str, since: str, logs_dir: Path | None = None, *,
                  any_tool: bool = False) -> dict[str, Any]:
    """Записи и запуски этого агента из журнала dev-mcp с момента прошлого хода."""
    logs_dir = logs_dir or ROOT / "logs"
    rows, unattributed, paths = [], 0, []
    # Текущие сутки пишутся в audit.jsonl, датированные — прошлые (15.11: без него ход был не виден).
    files = sorted(logs_dir.glob("audit-*.jsonl"))[-1:] + [logs_dir / "audit.jsonl"]
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines[-20000:]:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if since and str(d.get("ts", "")) <= since:
                continue
            tool = str(d.get("tool") or "")
            if not any_tool and tool not in ("write_file", "apply_patch", "run", "run_task", "edit_file", "move_path",
                            "delete_path", "fetch_url"):
                continue
            if str(d.get("agent") or "") == tag:
                args = d.get("args") if isinstance(d.get("args"), dict) else {}
                what = args.get("path") or args.get("cmd") or args.get("name") or args.get("root") or ""
                rows.append(f"{tool} {str(what)[:160]} -> {d.get('decision')}")
                if tool in ("write_file", "apply_patch", "edit_file") and args.get("path"):
                    paths.append(str(args["path"]))
            elif not d.get("agent"):
                unattributed += 1
    return {"actions": rows[-60:], "count": len(rows), "unattributed": unattributed,
            "paths": list(dict.fromkeys(paths))[-40:]}


def project_probes(project: str) -> list[dict[str, Any]]:
    try:
        doc = tomllib.loads((ROOT / "fleet-projects.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    raw = ((doc.get("projects") or {}).get(project) or {}).get("probe") or []
    return [p for p in raw if isinstance(p, dict) and isinstance(p.get("cmd"), list) and p.get("id")]


def touched_tests(probe: dict[str, Any], root: str, touched: list[str]) -> list[str]:
    """Тесты по файлам, которые тронул ЭТОТ агент: шаблоны `tests_for` ({stem} — имя файла без
    расширения), плюс сами тронутые тесты. Только существующие файлы под корнем."""
    base = Path(root)
    out: list[str] = []
    for raw in touched:
        path = Path(raw)
        try:
            rel = path.resolve().relative_to(base.resolve()) if path.is_absolute() else path
        except (OSError, ValueError):
            continue
        if rel.suffix != ".py":
            continue
        cands = ([rel] if rel.name.startswith("test_")
                 else [Path(t.format(stem=rel.stem)) for t in probe.get("tests_for") or []])
        for c in cands:
            if (base / c).is_file():
                out.append(c.as_posix())
    return list(dict.fromkeys(out))[:20]


def run_probes(project: str, root: str | None, budget_s: float = 240,
               touched: list[str] | None = None) -> list[dict[str, Any]]:
    out, started = [], time.monotonic()
    for p in project_probes(project):
        tests: list[str] = []
        if p.get("tests_for") is not None:
            tests = touched_tests(p, root or "", touched or [])
            if not tests:
                continue  # агент не трогал ничего, у чего есть тесты — пробы нет
            p = {**p, "cmd": [*p["cmd"], *tests]}
        left = budget_s - (time.monotonic() - started)
        if left < 10 or not root:
            break
        try:
            run = subprocess.run([str(x) for x in p["cmd"]], cwd=root, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace",
                                 timeout=min(float(p.get("timeout_s", 120)), left),
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            item = {"id": p["id"], "code": run.returncode,
                    "tail": ((run.stdout or "") + (run.stderr or ""))[-1500:]}
        except (OSError, subprocess.SubprocessError) as exc:
            item = {"id": p["id"], "code": -1, "tail": str(exc)[-500:]}
        if tests:
            item["tests"] = tests  # exact tests selected from this agent's modified files
        # Only explicit, locally configured segment links can prove an item done.
        for key in ("segment", "segment_id", "segments", "segment_ids"):
            if key in p:
                item[key] = p[key]
        image = p.get("image")
        if image:
            path = Path(root) / str(image)
            try:
                if path.is_file() and time.time() - path.stat().st_mtime < 600 and path.stat().st_size < 8_000_000:
                    item["image"] = {"mime": "image/png" if path.suffix.lower() == ".png" else "image/jpeg",
                                     "data": base64.b64encode(path.read_bytes()).decode("ascii")}
            except OSError:
                pass
        out.append(item)
    return out


# ---------------------------------------------------------------- decision-модель
_RELAY_DIR = ROOT.parent / "ai-relay"
_relay_started = [0.0]


def _refused(exc: BaseException) -> bool:
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, ConnectionRefusedError) or "10061" in str(reason)


def _start_relay() -> bool:
    """15.11: relay запускался только руками и лежал → решения уходили на Jev/владельцу.
    Порт отверг соединение — поднять `node relay.mjs` отдельно от dev-mcp (вне его Job Object,
    чтобы перезапуск dev не убил relay); не чаще раза в 5 мин. Автозапуск — C:/ops (gemini-relay)."""
    import shutil
    import subprocess

    now = time.monotonic()
    if os.environ.get("PYTEST_CURRENT_TEST") or RELAY_URL != "http://127.0.0.1:8790/v1beta/decisions":
        return False  # тесты и чужой адрес relay не поднимают
    if now - _relay_started[0] < 300 or not (_RELAY_DIR / "relay.mjs").is_file():
        return False
    _relay_started[0] = now
    node = shutil.which("node")
    if not node:
        return False
    flags = 0
    if os.name == "nt":
        # 21.11 (№116 п. 7): без DETACHED_PROCESS — с ним CREATE_NO_WINDOW не действует, и консольные
        # потомки relay открыли бы видимые окна. Своя скрытая консоль; вне Job Object — BREAKAWAY ниже.
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    for extra in ((0x01000000,) if os.name == "nt" else ()) + (0,):  # CREATE_BREAKAWAY_FROM_JOB, затем без
        try:
            subprocess.Popen([node, "relay.mjs"], cwd=str(_RELAY_DIR), creationflags=flags | extra,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True)
            return True
        except OSError:
            continue
    return False


def _relay_decide(blocks: list[dict[str, Any]], questions: dict[str, Any],
                  timeout_s: float = 60) -> dict[str, Any] | None:
    if _redact_text("overseer-redaction-health") == "[REDACTION_UNAVAILABLE]":
        return None  # never send even image blocks without the trusted sanitizer
    body = json.dumps({"model": MODEL, "input": _safe_blocks(blocks), "questions": _scrub(questions)},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(RELAY_URL, data=body, method="POST",
                                 headers={"content-type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout_s) as r:
            answers = json.loads(r.read()).get("answers")
    except (OSError, ValueError, AttributeError) as exc:
        if _refused(exc):
            _start_relay()  # следующий вызов уже пойдёт в поднятый relay; этот — на запасной Jev
        return None
    return answers if isinstance(answers, dict) else None


def _jev_decide(blocks: list[dict[str, Any]], questions: dict[str, Any]) -> dict[str, Any] | None:
    """Запасной маршрут — Jev System One мельницы (текст без картинок, критерии true/false)."""
    import sys
    if _redact_text("overseer-redaction-health") == "[REDACTION_UNAVAILABLE]":
        return None
    mill = ROOT.parent / "mill-works"
    if not mill.is_dir():
        return None
    if str(mill) not in sys.path:
        sys.path.insert(0, str(mill))
    try:
        from mill import jev  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    conv = {}
    for k, q in questions.items():
        c = q.get("criteria")
        if q["type"] == "noul" and isinstance(c, dict):
            c = {"true": c.get("true_description", ""), "false": c.get("false_description", "")}
        conv[k] = {**q, "criteria": c} if c is not None else dict(q)
    text = "".join(b.get("text", "") for b in _safe_blocks(blocks))
    return jev.ask_system_one({"evidence": text[:120000]}, _scrub(conv))


def decide(blocks: list[dict[str, Any]], questions: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    answers = _relay_decide(blocks, questions)
    if answers is not None:
        return answers, MODEL
    answers = _jev_decide(blocks, questions)
    return answers, ("jev" if answers is not None else "")


def _p(answers: dict[str, Any], key: str) -> float | None:
    try:
        v = float(answers[key]["noul"])
        return v if 0 <= v <= 1 else None
    except (KeyError, TypeError, ValueError):
        return None


def _choice(answers: dict[str, Any], key: str) -> tuple[str, float]:
    try:
        probs = answers[key]["probabilities"]
        best = max(probs, key=lambda k: float(probs[k]))
        return str(best), float(probs[best])
    except (KeyError, TypeError, ValueError, AttributeError):
        return "", 0.0


def _noul(instructions: str, yes: str, no: str) -> dict[str, Any]:
    return {"type": "noul", "instructions": instructions,
            "criteria": {"true_description": yes, "false_description": no}}


def build_questions(card: dict[str, Any], history: dict[str, Any], probes: list[dict[str, Any]],
                    reply_sentences: list[str]) -> dict[str, Any]:
    q: dict[str, Any] = {}
    for s in card["segments"]:
        if _non_task(s["text"]):
            continue
        if not s["kind"]:
            q[f"kind_{s['id']}"] = {"type": "choice", "criteria": KINDS, "instructions":
                                    f"Сегмент {s['id']} текста владельца (TASK.segments.{s['id']}) — что это?"}
            q[f"proof_{s['id']}"] = {"type": "choice", "criteria": PROOFS, "instructions":
                                     f"Чем проверяется выполнение сегмента {s['id']} текста владельца?"}
    for s in card["segments"]:
        if not _non_task(s["text"]) and s["status"] == "open" and (s["kind"] in WORK_KINDS or not s["kind"]):
            q[f"done_{s['id']}"] = _noul(
                f"Сегмент {s['id']} текста владельца выполнен к концу этого хода? Опирайся на "
                "TURN.history (что агент реально изменил и запускал) и TURN.probes (вывод проверок, "
                "снимки экрана). Вопрос закрыт, если в TURN.reply на него дан ответ по существу. "
                "Слова исполнителя о сделанном доказательством не считаются.",
                "есть изменение, вывод проверки, снимок или ответ, которые выполняют сегмент",
                "следов выполнения нет или есть только заявление исполнителя")
            q[f"claims_{s['id']}"] = _noul(
                f"Исполнитель в TURN.reply заявляет сегмент {s['id']} сделанным или работающим?",
                "заявляет", "не заявляет")
    q["claims_done"] = _noul("Исполнитель в TURN.reply заявляет, что работа или её часть готова/работает?",
                             "заявляет", "не заявляет")
    q["on_task"] = _noul(
        "Работа этого хода (TURN.reply, TURN.history) реально продвигает TASK.main_goal? "
        "Занятость мелким последним пунктом без продвижения основной цели — уход в сторону. "
        "TASK.current_stage — лишь этап основной цели, не её замена.",
        "продвигает главную цель владельца", "уводит в мелочи или в сторону")
    q["breaks_constraint"] = _noul("Ход нарушает какое-либо ограничение из TASK?",
                                   "нарушает", "не нарушает")
    q["asks_owner"] = _noul("Исполнитель в конце TURN.reply просит у человека решение, данные или действие "
                            "и ждёт ответа?", "просит и ждёт", "не просит, продолжает сам")
    q["repeat"] = _noul("TURN.reply по существу повторяет PREV.reply, без нового продвижения?",
                        "повтор", "есть новое")
    if not history.get("count"):
        q["concrete"] = _noul("В ходе есть предметное действие (изменён файл, выполнена команда, получен "
                              "вывод), а не только рассуждение или план?", "есть действие", "только слова")
    seg_opts = {s["id"]: s["text"][:300] for s in card["segments"] if not _non_task(s["text"])}
    q["answer_src"] = {"type": "choice", "criteria": {**seg_opts, "none": "в тексте владельца ответа нет"},
                       "instructions": "Если исполнитель задаёт вопрос, какой сегмент TASK на него отвечает?"}
    possible = [s for s in card["segments"]
                if s["status"] == "open" and (s["kind"] in WORK_KINDS or not s["kind"])
                and not _non_task(s["text"])]
    if (len(possible) > 1 and any(_NUM.match(s["text"]) for s in possible)
            and possible[0]["origin"] == "first" and not _NUM.match(possible[0]["text"])):
        possible = possible[1:]
    open_ids = {s["id"]: s["text"][:300] for s in possible}
    if len(open_ids) >= 2:
        q["next_item"] = {"type": "choice", "criteria": open_ids, "instructions":
                          "Выбери ОДИН следующий предметный шаг, который продвигает TASK.main_goal. "
                          "Учитывай TASK.current_stage, порядок владельца и фактический результат TURN. "
                          "Не выбирай реплику «Продолжи», техническую переписку или мелочь ради мелочи. "
                          "Если явного приоритета нет — вернись к первому открытому требованию."}
    if len(reply_sentences) >= 2:
        q["now_doing"] = {"type": "choice", "criteria": {f"r{i}": t[:300] for i, t in enumerate(reply_sentences)},
                          "instructions": "Какое предложение TURN.reply описывает, что исполнитель делает сейчас?"}
    return q


def _sentences(text: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+|\n+", text or "") if len(p.strip()) > 15]
    return parts[:40]


def _addressed_success(segment: dict[str, Any], probes: list[dict[str, Any]]) -> bool:
    """A generic green build cannot automatically close unrelated card items."""
    sid = segment["id"]
    for probe in probes:
        if type(probe.get("code")) is not int or probe["code"] != 0:
            continue
        ids = [probe.get("id"), probe.get("segment"), probe.get("segment_id")]
        for field in ("segments", "segment_ids"):
            linked = probe.get(field)
            if isinstance(linked, list):
                ids.extend(linked)
        if sid in ids:
            return True
    return False


def _can_close(segment: dict[str, Any], probes: list[dict[str, Any]]) -> bool:
    proof = segment.get("proof")
    if proof in ("code", "probe", "live"):
        return _addressed_success(segment, probes)
    # Screenshots are deliberately not sent to the model until pixel-level
    # sanitization exists; a green exit code alone cannot verify their content.
    if proof == "visual":
        return False
    # Answer-only questions are resolved in the conversation, not by executing code.
    return proof == "answer" and segment.get("kind") == "question"


# ---------------------------------------------------------------- правила
def _quote(text: str, n: int = 220) -> str:
    t = " ".join(str(text).split())
    return "«" + (t if len(t) <= n else t[:n].rstrip() + "…") + "»"


def verdict(card: dict[str, Any], answers: dict[str, Any], probes: list[dict[str, Any]],
            history: dict[str, Any], reply_sentences: list[str]) -> tuple[str, str]:
    seg = {s["id"]: s for s in card["segments"]}
    open_work = _work_items(card)
    goal = _focus_quote(_goal(card))
    agent_work = [s for s in _next_items(card) if s["proof"] not in ("live", "owner")]
    nxt, _ = _choice(answers, "next_item")
    target = next((s for s in agent_work if s["id"] == nxt), None)
    target = target or (agent_work[0] if agent_work else None)
    next_step = _focus_quote(target["text"], 180) if target else goal
    focus = f"Главная цель: {goal}. Следующий шаг: {next_step}."
    claims_done = (_p(answers, "claims_done") or 0) >= CLAIM_MIN
    failed = [p for p in probes if p.get("code") not in (0, None)]
    if failed and claims_done:
        p = failed[0]
        return "not_working", (f"{MARK} Заявлено, что работает. Проба `{p['id']}` вернула {p['code']}:\n"
                               f"{p['tail'][-600:]}\nИсправь и покажи вывод.")
    if card["idle_turns"] >= STALL_TURNS or card["repeat_turns"] >= 2 or \
            any(v >= 2 for v in card["probe_errors"].values()):
        return "stop_stuck", ""
    # A model's asks_owner score without a question in the actual reply is not
    # permission to stop a long-running task and repeatedly ping the owner.
    owner_question = any("?" in s or "ВОПРОС ВЛАДЕЛЬЦУ:" in s for s in reply_sentences)
    if (_p(answers, "asks_owner") or 0) >= FLAG_MIN and owner_question:
        src, conf = _choice(answers, "answer_src")
        if src in seg and conf >= 0.6:
            return "answer_from_task", f"{MARK} Ответ уже есть в задаче: {_quote(seg[src]['text'], 500)}. {focus}"
        return "stop_owner", ""
    on_task = _p(answers, "on_task")
    if (on_task is not None and on_task < 1 - FLAG_MIN) or (_p(answers, "breaks_constraint") or 0) >= FLAG_MIN:
        return "drift", f"{MARK} Ход не продвинул главную цель. {focus}"
    # Заявление «готово» проверяется раньше «пустого хода»: «всё сделано» без единого действия —
    # это «а чё забыл?» владельца, а не просьба назвать файл.
    live_claims = [s for s in open_work if s["proof"] in ("live", "owner")
                   and (_p(answers, f"claims_{s['id']}") or 0) >= CLAIM_MIN]
    if live_claims:
        s = live_claims[0]
        return "not_believed", (f"{MARK} Главная цель: {goal}. Заявлен результат по "
                                f"{_focus_quote(s['text'], 160)}, но нужна живая проверка. "
                                f"Следующий шаг: получи живое доказательство для этого пункта.")
    if claims_done and agent_work:
        return "forgotten", (f"{MARK} Заявлен результат без подтверждения всей работы. "
                             f"{focus} Покажи проверку этого шага.")
    # 16.11 (владелец): ход без изменений — не улика проблемы (агент читал, думал или просто
    # продолжает); требование «назови файл и команду» сбивало агента на лишнюю работу. Без улики
    # надзор не требует, а продолжает к следующему несделанному пункту (ниже) или молчит.
    if not agent_work:
        return "stop_look", ""
    return "continue", f"{MARK} {focus}"


def protocol_message(card: dict[str, Any]) -> str:
    return (f"{MARK} Этот чат под автоматическим надзором. Сообщения с меткой {MARK} — не новые решения "
            "владельца, а возврат к его исходному тексту; решения владельца — только в его собственных "
            f"сообщениях. Во ВСЕХ вызовах инструментов dev-mcp передавай agent=\"{card['tag']}\". "
            "Вопрос к человеку пиши отдельной строкой «ВОПРОС ВЛАДЕЛЬЦУ: …».")


# ---------------------------------------------------------------- 21.11: надзор «за владельца»
_PROXY_PATH = Path(__file__).with_name("overseer_proxy.py")
_proxy_cache: dict[str, Any] = {}


def _proxy_module() -> Any:
    """overseer_proxy.py перечитывается при смене mtime — его правки не требуют перезапуска dev-mcp.

    Сломанная новая версия не роняет надзор: остаётся последняя загруженная.
    """
    stamp = _PROXY_PATH.stat().st_mtime_ns
    if _proxy_cache.get("stamp") != stamp:
        try:
            spec = importlib.util.spec_from_file_location("dev_mcp.fleet._overseer_proxy_live", _PROXY_PATH)
            module = importlib.util.module_from_spec(spec)
            # 25.11: исходник компилируется напрямую — .pyc с той же секундой и размером давал старую версию.
            code = compile(_PROXY_PATH.read_bytes(), str(_PROXY_PATH), "exec")
            exec(code, module.__dict__)
        except Exception:
            if "module" not in _proxy_cache:
                raise
            _proxy_cache["stamp"] = stamp
            return _proxy_cache["module"]
        _proxy_cache.update(stamp=stamp, module=module)
    return _proxy_cache["module"]


# ---------------------------------------------------------------- ход
def observe(book: Book, snapshot: dict[str, Any], *,
            decide_fn: Callable[..., tuple[dict[str, Any] | None, str]] = decide,
            history_fn: Callable[..., dict[str, Any]] = agent_history,
            probes_fn: Callable[..., list[dict[str, Any]]] = run_probes,
            root_fn: Callable[[str], str | None] | None = None,
            notify_fn: Callable[[str], Any] | None = None,
            fleet_fn: Callable[[str, str], str | None] | None = None) -> dict[str, Any]:
    cid = chat_id(snapshot.get("url", ""))
    entry = book.chats().get(cid)
    if not entry or entry.get("mode") not in ("shadow", "send"):
        return {"action": "none", "why": "not_watched"}
    users = [str(u) for u in snapshot.get("user_messages") or [] if str(u).strip()]
    reply = str(snapshot.get("last_assistant") or "")
    if snapshot.get("generating") or snapshot.get("blocked") or not snapshot.get("composer_empty", True):
        return {"action": "none", "why": "busy"}
    if not users:
        return {"action": "none", "why": "no_task"}
    # save_card/log sanitize *all* stored fields. If Mill Works's redactor
    # fails, persisting placeholders would corrupt durable card keys/values.
    if _redact_text("overseer-redaction-health") == "[REDACTION_UNAVAILABLE]":
        # Without the sibling sanitizer neither a relay nor a local model is a
        # usable backend: fail closed before any transcript or probe is sent.
        status = (NO_DECISION_BACKEND if not (ROOT.parent / "mill-works").is_dir()
                  else NO_REDACTOR)
        with _lock:
            _set_backend_status(book, cid, status)
        return {"action": "none", "why": status}
    if entry.get("status") == NO_DECISION_BACKEND and entry.get("engine") == "proxy":
        # Missing Jev with a working redactor: an explicit re-mark allows
        # retry after installation; no per-turn attempts or pauses.
        return {"action": "none", "why": NO_DECISION_BACKEND}
    with _lock:
        if entry.get("status"):
            _set_backend_status(book, cid, "")
        card = book.card(cid) or new_card(cid, entry, users[0])
        if not card.get("turns") and "pending_owner_replies" not in card:
            card["pending_owner_replies"] = [users[0]]
        sent = set(card["sent"])
        known = {s["sha"] for s in card["sources"]}
        for u in users[1:]:
            h = _sha(u)
            if h in sent or h in known or u.startswith(MARK):
                continue
            ids = _add_source(card, u, "owner")
            card.setdefault("pending_owner_replies", []).append(u)
            book.log({"chat": cid, "event": "owner_reply", "after_verdict": card["last_verdict"],
                      "text": u[:2000], "segments": ids})
        book.save_card(card)
    if snapshot.get("last_role") != "assistant" or not reply.strip():
        return {"action": "none", "why": "not_assistant_turn"}
    turn = _sha(f"{snapshot.get('n_messages')}|{reply}")
    if entry.get("engine") == "proxy":
        # 25.11 (jev-1): идемпотентность по (chat, owner_revision, turn_id) ведёт сам proxy — он
        # возвращает то же недоставленное решение при повторном обращении и «seen» после него.
        import sys
        try:
            proxy = _proxy_module()
        except Exception as error:  # noqa: BLE001 - нет модуля = молчание, а не падение инструмента
            book.log({"chat": cid, "event": "proxy_unavailable",
                      "error": f"{type(error).__name__}: {error}"[:300]})
            return {"action": "none", "why": "proxy_unavailable"}
        kwargs = {"fleet_fn": fleet_fn} if getattr(proxy, "VERSION", "") != "proxy-1" else {}
        return proxy.observe_turn(sys.modules[__name__], book, card, entry, users, reply, turn,
                                  snapshot, notify_fn, **kwargs)
    if turn == card["last_turn"]:
        return {"action": "none", "why": "seen"}
    return _semantic_observe(book, card, entry, users, reply, turn, snapshot,
                             history_fn, probes_fn, root_fn, notify_fn)

def proxy_wake(book: Book, cid: str, fleet_fn: Callable[[str, str], str | None] | None) -> str:
    """Токен повторной оценки ждущего чата (fleet-блокер снят); без вызова Jev. Пусто — ждать."""
    try:
        if (book.chats().get(cid) or {}).get("status"):
            return ""
        proxy = _proxy_module()
        check = getattr(proxy, "wake_check", None)
        if not check:
            return ""
        with _lock:
            return str(check(book, cid, fleet_fn) or "")
    except Exception:  # noqa: BLE001 - список чатов не должен падать из-за проверки
        return ""


def _root(project: str) -> str | None:
    from .project_config import project_root
    return project_root(project)

# ---------------------------------------------------------------- 18.11: смысловой генеративный ход
_LOCAL_GENERATION_URL = "http://127.0.0.1:11435/v1/chat/completions"
_INTERVENTIONS = {"drift", "forgotten", "not_working", "not_believed",
                  "answer_from_task", "stop_stuck", "stop_owner"}
_STOPS = {"stop_stuck", "stop_owner"}


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", str(text).casefold(), flags=re.UNICODE)


def _norm(text: str) -> str:
    return " ".join(_words(text))


def _owner_goals(users: list[str]) -> list[str]:
    """Extract substantive owner instructions across the conversation."""
    commands = re.compile(
        r"\b(?:исправь|сделай|создай|добавь|подготовь|продолжи\s+(?:именно\s+)?"
        r"разработку|прочитай|перечитай|составь|проверь|реализуй|нужно|цель|"
        r"запусти|исследуй|документируй|поставь|проведи|найди|организуй|"
        r"делай|работай|делаем\s+без\s+ведущего|переключи|отмени|останови)\b",
        re.IGNORECASE)
    out = []
    for raw in users:
        s = str(raw).strip()
        # Transport meta does not become the objective; preserve the clause
        # after "твоя цель" from the real owner's last substantive instruction.
        goal = re.search(r"(?:твоя|ваша)\s+цель\s*[:—-]\s*", s, re.IGNORECASE)
        if goal:
            s = s[goal.end():].strip()
        elif re.search(r"^\s*(?:сейчас\s+я\s+ставлю\s+тебя\s+на\s+надзор|"
                       r"я\s+иду\s+спать)", s, re.IGNORECASE):
            continue
        s = re.sub(r"(?i)^\S+\.(?:jsonl?|txt)\s*(?:code|document)\s*", "", s).strip()
        if not s or _non_task(s) or s.startswith(MARK):
            continue
        if (re.search(r"(?i)зарегистрировал\s+два\s+аккаунта", s)
                and "что нужно сделать" in s.casefold()):
            continue  # account-status question precedes the actual assignment
        if not commands.search(s) and len(_words(s)) < 24:
            continue  # account status, console chrome, small talk
        out.append(s[:10000])
    return out[-24:]


def _repeated_promise(previous: str, current: str) -> bool:
    """Conservative repeat of a future-tense status rather than a new result."""
    if not previous or not current or len(previous) > 750 or len(current) > 750:
        return False
    plans = re.compile(r"(?i)\b(?:проверю|заберу|продолжу|продолжаю|затем|планирую|сначала)\b")
    results = re.compile(r"(?i)\b(?:проверено|готово|создано|запущено|исправлено|пройдено|"
                         r"подтверждено|найдено|выполнено|тесты прошли)\b")
    if not plans.search(previous) or not plans.search(current) or results.search(current):
        return False
    before = {word[:5] for word in _semantic_tokens(previous)}
    after = {word[:5] for word in _semantic_tokens(current)}
    return len(before) >= 7 and len(after) >= 7 and len(before & after) >= 6 and (
        len(before & after) / min(len(before), len(after)) >= 0.42)


def _short_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    """Reduce expensive Qwen context without inventing summaries or changing guard input.

    The verifier still receives the full owner instructions and assistant reply.
    Keep both the original goal and the latest owner correction, as well as the
    agent's opening and closing result (long reports often contain both).
    """
    def ends(text: str, first: int, last: int) -> str:
        value = str(text)
        if len(value) <= first + last:
            return value
        return value[:first].rsplit(" ", 1)[0] + "\n…\n" + value[-last:].split(" ", 1)[-1]

    goals = [str(g) for g in evidence.get("owner_instructions") or []]
    selected = []
    if goals:
        original = goals[0]
        explicit = re.search(r"(?:твоя|ваша)\s+цель\s*[:—-]\s*", original, re.IGNORECASE)
        selected.append(original[explicit.end():explicit.end() + 440] if explicit
                        else ends(original, 210, 140))
        if len(goals) > 1:
            selected.append(ends(goals[-1], 240, 140))
    return {
        "owner_instructions": selected,
        "assistant_reply": ends(str(evidence.get("assistant_reply") or ""), 430, 400),
        "previous_assistant_reply": str(evidence.get("previous_assistant_reply") or "")[-260:],
        "latest_owner_turn": str(evidence.get("latest_owner_turn") or "")[-140:],
        "repeated_promise": _repeated_promise(
            str(evidence.get("previous_assistant_reply") or ""),
            str(evidence.get("assistant_reply") or "")),
        "agent_actions": [str(a)[:110] for a in (evidence.get("agent_actions") or [])[-3:]],
        "probes": (evidence.get("probes") or [])[-2:],
    }


def _grounded_quote(source: str, *, owner: bool = False) -> str:
    """Take a contiguous 3–8-word literal span, never generated by the model."""
    value = str(source)
    if owner:
        match = re.search(r"(?:твоя|ваша)\s+цель\s*[:—-]\s*", value, re.IGNORECASE)
        if match:
            value = value[match.end():]
    else:
        match = re.search(r"\b(?:продолжаю|проверю|заберу|затем)\b", value, re.IGNORECASE)
        if match:
            value = value[match.start():]
    words = list(re.finditer(r"\w+", value))
    if len(words) < 3:
        return ""
    return value[words[0].start():words[min(8 if owner else 12, len(words)) - 1].end()]


def _generate_repeat_nudge(evidence: dict[str, Any], *,
                           diagnostics: Callable[[dict[str, Any]], None] | None = None
                           ) -> dict[str, Any] | None:
    """Qwen writes only the nudge; deterministic, literal quotes ground its verdict."""
    goals = list(evidence.get("owner_instructions") or [])
    reply = str(evidence.get("assistant_reply") or "")
    if not goals:
        return None
    owner_quote = _grounded_quote(str(goals[0]), owner=True)
    reply_quote = _grounded_quote(reply)
    if not owner_quote or not reply_quote:
        return None
    prompt = (
        "/no_think Агент в двух соседних завершённых сообщениях снова ОБЕЩАЕТ "
        "проверить одно и то же, но не сообщает результата. "
        "Составь ОДНУ конкретную повелительную инструкцию агенту, не от первого лица. "
        "Начни с 'Покажи' или 'Укажи'. Попроси результат обещанной проверки "
        "или точную причину блокировки. Упомяни не меньше двух слов из reply_quote. "
        "Не утверждай, что тесты не запускались: это неизвестно. "
        "Ответ СТРОГО JSON вида {\"message\":\"Покажи ...\"}, других полей нет."
    )
    context = {"reply_quote": reply_quote, "reply": reply[:350],
               "previous_reply": str(evidence.get("previous_assistant_reply") or "")[-250:]}
    sanitized = json.dumps(_scrub(context), ensure_ascii=False)
    if "[REDACTION_UNAVAILABLE]" in sanitized:
        return None
    body = {"model": "qwen3-4b", "messages": [
        {"role": "system", "content": prompt},
        {"role": "user", "content": sanitized}],
        "temperature": 0.1, "max_tokens": 130, "stream": False}
    req = urllib.request.Request(
        _LOCAL_GENERATION_URL, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST", headers={"content-type": "application/json"})
    started = time.monotonic()
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=50) as response:
            payload = json.load(response)
        choice = payload["choices"][0]
        if diagnostics:
            diagnostics({"route": "repeat_nudge", "elapsed_s": round(time.monotonic() - started, 1),
                         "finish_reason": choice.get("finish_reason", ""),
                         "completion_tokens": (payload.get("usage") or {}).get("completion_tokens")})
        data = json.loads(choice["message"]["content"])
        message = str(data["message"]).strip()
        if not re.match(r"(?i)^(?:покажи|укажи|приведи|представь)\b", message):
            return None
        return {"verdict": "drift", "goal": owner_quote,
                "goal_quote": owner_quote, "reply_summary": reply_quote,
                "reply_quote": reply_quote, "issue": "повтор обещания без результата",
                "message": message, "confidence": 0.92}
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError) as error:
        if diagnostics:
            diagnostics({"route": "repeat_nudge", "elapsed_s": round(time.monotonic() - started, 1),
                         "error": type(error).__name__})
        return None


def _local_generate(evidence: dict[str, Any], *,
                    diagnostics: Callable[[dict[str, Any]], None] | None = None
                    ) -> dict[str, Any] | None:
    """Only local Qwen3-4B via second-PC bridge. Unavailable means silence.

    Optional diagnostics expose timing and token counts, never private content.
    """
    compact = _short_evidence(evidence)
    if compact["repeated_promise"] and re.search(
            r"(?i)\bпродолжи\b", str(evidence.get("latest_owner_turn") or "")):
        return _generate_repeat_nudge(evidence, diagnostics=diagnostics)
    system = (
        "/no_think Сравни завершённый ответ агента с целью владельца (учти последнее уточнение). "
        "Шаг 1: есть ли ЯВНАЯ вредная ошибка, дрейф, беспричинная остановка или ложное 'готово'? "
        "Если нет прямого доказательства — верни РОВНО {\"verdict\":\"continue\"}. "
        "Не объясняй вердикт continue, никаких других полей! "
        "Исследование, план и отсутствие команд сами по себе НЕ ошибка. "
        "Исключение: repeated_promise=true означает два одинаковых обещания без результата: "
        "нужен drift с конкретным требованием одного проверяемого результата, не continue. "
        "Не считай 'продолжи', надзор или логи целью. "
        "Шаг 2 ТОЛЬКО при явной проблеме верни короткий JSON с полями "
        "verdict, goal, goal_quote, reply_summary, reply_quote, issue, message, confidence. "
        "verdict: drift/forgotten/not_working/not_believed/answer_from_task/stop_stuck/stop_owner. "
        "goal_quote — ДОСЛОВНЫЕ 3-12 слов owner_instructions, reply_quote — "
        "ДОСЛОВНЫЕ 3-14 слов assistant_reply. goal и reply_summary — до 12 слов каждый; "
        "issue — до 6 слов; message — одна конкретная инструкция с двумя словами "
        "из reply_quote без длинных цитат владельца; confidence 0.9-1 только если уверен."
    )
    sanitized = json.dumps(_scrub(_short_evidence(evidence)), ensure_ascii=False)
    if "[REDACTION_UNAVAILABLE]" in sanitized:
        return None  # never ask Qwen to judge placeholder text
    body = {"model": "qwen3-4b", "messages": [
        {"role": "system", "content": system},
        {"role": "user", "content": sanitized}],
        "temperature": 0.1, "max_tokens": 360, "stream": False}
    req = urllib.request.Request(
        _LOCAL_GENERATION_URL, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST", headers={"content-type": "application/json"})
    started = time.monotonic()
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=50) as response:
            payload = json.load(response)
        choice = payload["choices"][0]
        content = choice["message"]["content"]
        if diagnostics:
            diagnostics({
                "elapsed_s": round(time.monotonic() - started, 1),
                "finish_reason": choice.get("finish_reason", ""),
                "completion_tokens": (payload.get("usage") or {}).get("completion_tokens"),
                "reasoning_chars": len(choice["message"].get("reasoning_content") or ""),
                "content_chars": len(content or ""),
            })
        content = re.sub(r"(?s)<think>.*?</think>", "", content).strip()
        content = content.strip(chr(96) + " \n\r\t")
        if content.startswith("json"):
            content = content[4:].strip()
        result = json.loads(content)
        return result if isinstance(result, dict) else None
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError) as error:
        if diagnostics:
            diagnostics({"elapsed_s": round(time.monotonic() - started, 1),
                         "error": type(error).__name__})
        return None


def _semantic_tokens(value: str) -> set[str]:
    stop = {"что", "это", "для", "как", "его", "она", "или", "при", "без", "есть",
            "надо", "нужно", "только", "сейчас", "после", "когда", "этот", "этого"}
    return {w for w in _words(value) if len(w) > 3 and w not in stop}


def _copied_owner_span(message: str, users: list[str]) -> bool:
    words = _words(message)
    if len(words) < 13:
        return False
    for user in users:
        owner = _words(user)
        spans = {tuple(owner[i:i + 13]) for i in range(len(owner) - 12)}
        if any(tuple(words[j:j + 13]) in spans for j in range(len(words) - 12)):
            return True
    return False


def _grounding_error(g: Any, goals: list[str], users: list[str], reply: str) -> str:
    if not isinstance(g, dict):
        return "generation_unavailable"
    verdict_name = g.get("verdict")
    if verdict_name == "continue":
        return "no_useful_intervention"
    if verdict_name not in _INTERVENTIONS:
        return "invalid_verdict"
    try:
        if float(g.get("confidence", 0)) < 0.85:
            return "low_confidence"
    except (TypeError, ValueError):
        return "low_confidence"
    quote = _norm(str(g.get("goal_quote") or ""))
    if not 3 <= len(_words(quote)) <= 12 or not any(quote in _norm(s) for s in goals):
        return "ungrounded_owner_goal"
    goal_text = str(g.get("goal") or "").strip()
    if not goal_text or len(goal_text) > 350:
        return "invalid_goal"
    # A literal quote alone does not ground the model's *interpretation* of it.
    # Require both the reconstructed owner goal and reply summary to refer to
    # their respective quoted evidence, including Russian inflections.
    def stems(value: str) -> set[str]:
        return {word[:5] for word in _semantic_tokens(value)}
    if not (stems(goal_text) & stems(quote)):
        return "ungrounded_goal_summary"
    anchor = _norm(str(g.get("reply_quote") or ""))
    if not 3 <= len(_words(anchor)) <= 14 or anchor not in _norm(reply):
        return "ungrounded_agent_turn"
    summary = str(g.get("reply_summary") or "").strip()
    if not summary or len(summary) > 350:
        return "invalid_reply_summary"
    if not (stems(summary) & stems(anchor)):
        return "ungrounded_reply_summary"
    message = str(g.get("message") or "").strip()
    if not message or len(message) > 500 or MARK in message:
        return "invalid_message"
    if len(re.split(r"(?<=[.!?])\s+", message)) > 3:
        return "too_many_sentences"
    if len(stems(message) & stems(anchor)) < 2:
        return "no_reply_reference"
    if _copied_owner_span(message, users):
        return "owner_quote_too_long"
    # A true-looking statement or copy of the owner's task is not guidance.
    # Require one explicit, executable next action. False negatives are
    # preferable to sending a fragmentary or accusatory Qwen output.
    if not re.search(
            r"(?i)(?:^|[.!?]\s+)(?:покажи|укажи|назови|приведи|представь|"
            r"запусти|проверь|перечисли|оформи|исправь|подготовь|"
            r"сверь|объясни|уточни|проведи|добавь|подтверди|"
            r"покажите|укажите|назовите|проверьте|представьте)\b", message):
        return "non_actionable_message"
    if not str(g.get("issue") or "").strip() or len(str(g["issue"])) > 180:
        return "missing_issue"
    return ""


def _duplicate_issue(card: dict[str, Any], owner_key: str, g: dict[str, Any]) -> bool:
    a, m = _semantic_tokens(str(g["issue"])), _semantic_tokens(str(g["message"]))
    for prev in card.get("sent_semantics", []):
        if prev.get("owner_key") != owner_key:
            continue
        b, n = _semantic_tokens(prev.get("issue", "")), _semantic_tokens(prev.get("message", ""))
        issue_jaccard = len(a & b) / max(1, len(a | b))
        message_jaccard = len(m & n) / max(1, len(m | n))
        if issue_jaccard >= 0.6 or message_jaccard >= 0.72 or prev.get("verdict") == g.get("verdict"):
            return True
    return False


def _evaluate_semantics(generated: dict[str, Any] | None, goals: list[str], users: list[str],
                        reply: str, probes: list[dict[str, Any]], card: dict[str, Any],
                        owner_key: str) -> tuple[str, str]:
    """Shared live/replay guardrails. Mutates only the supplied in-memory card."""
    reason = _grounding_error(generated, goals, users, reply) if goals else "no_owner_goal"
    name = str(generated.get("verdict", "silent")) if isinstance(generated, dict) else "silent"
    if not reason and name == "stop_owner" and "?" not in reply and "ВОПРОС ВЛАДЕЛЬЦУ:" not in reply:
        reason = "owner_not_asked"
    if not reason and name == "not_working" and not any(p.get("code") not in (0, None) for p in probes):
        reason = "no_failed_probe"
    issue = _norm(str(generated.get("issue", ""))) if isinstance(generated, dict) else ""
    if issue and issue == card.get("last_issue") and owner_key == card.get("last_issue_owner"):
        card["repeat_turns"] = card.get("repeat_turns", 0) + 1
    else:
        card["repeat_turns"] = 0
    card["last_issue"], card["last_issue_owner"] = issue, owner_key
    if not reason and name == "stop_stuck":
        failed_twice = any(
            p.get("code") not in (0, None) and
            card.get("probe_errors", {}).get(f"{p['id']}:{_sha(p.get('tail', '')[-300:])}", 0) >= 1
            for p in probes)
        if card["repeat_turns"] < 2 and not failed_twice:
            reason = "stuck_not_proven"
    for p in probes:
        if p.get("code") not in (0, None):
            key = f"{p['id']}:{_sha(p.get('tail', '')[-300:])}"
            card.setdefault("probe_errors", {})[key] = card.get("probe_errors", {}).get(key, 0) + 1
    if not reason and _duplicate_issue(card, owner_key, generated):
        reason = "duplicate_semantics"
    if not reason and name in _STOPS and card.get("owner_notice_key") == owner_key:
        reason = "owner_already_notified"
    return name, reason


def _semantic_observe(book: Book, card: dict[str, Any], entry: dict[str, Any],
                      users: list[str], reply: str, turn: str, snapshot: dict[str, Any],
                      history_fn: Callable[..., dict[str, Any]],
                      probes_fn: Callable[..., list[dict[str, Any]]],
                      root_fn: Callable[[str], str | None] | None,
                      notify_fn: Callable[[str], Any] | None) -> dict[str, Any]:
    """No quote templates, no fallback from failed generation to probability indexes."""
    cid = card["chat"]
    goals = _owner_goals(users)
    owner_key = _sha("\n".join(goals))
    history = history_fn(card["tag"], card["last_turn_at"])
    root = (root_fn or _root)(card["project"]) if card["project"] else None
    if not history.get("count"):
        probes = []
    elif probes_fn is run_probes:
        probes = run_probes(card["project"], root, touched=history.get("paths") or [])
    else:
        probes = probes_fn(card["project"], root)
    evidence = {
        "owner_instructions": goals,
        "assistant_reply": reply[-12000:],
        "previous_assistant_reply": str(snapshot.get("prev_assistant") or "")[-5000:],
        "latest_owner_turn": next((u for u in reversed(users) if not u.startswith(MARK)), ""),
        "agent_actions": (history.get("actions") or [])[-25:],
        "probes": [{k: v for k, v in p.items() if k != "image"} for p in probes]}
    generated = _local_generate(evidence) if goals else None
    with _lock:
        card = book.card(cid) or card
        name, reason = _evaluate_semantics(generated, goals, users, reply, probes, card, owner_key)
        card["turns"] += 1
        card["last_turn"], card["last_turn_at"], card["last_verdict"] = turn, _now(), name
        mode = entry["mode"]
        message = _redact_text(f"{MARK} {generated['message']}") if not reason else ""
        if message == "[REDACTION_UNAVAILABLE]":
            reason, message = "redaction_unavailable", ""
        if not reason and mode == "send":
            card.setdefault("sent", []).append(_sha(message))
            card.setdefault("sent_semantics", []).append(
                {"owner_key": owner_key, "issue": generated["issue"],
                 "message": generated["message"], "verdict": name})
            card["sent_semantics"] = card["sent_semantics"][-30:]
            if name in _STOPS:
                card["owner_notice_key"] = owner_key
        book.archive_turn(cid, {
            "turn": turn, "mode": mode, "reply": _archive_redact(reply)[-6000:],
            "owner_replies": _recent_owner_replies(card.get("pending_owner_replies") or []),
            "verdict": name, "sent_text": message if mode == "send" and not reason else "",
            "skipped_reason": reason or ("shadow" if mode != "send" else ""),
            "candidate_text": message if not reason else "",
            "agent_actions": [str(a)[:300] for a in evidence["agent_actions"]],
            "probes": [{k: (str(v)[-400:] if k == "tail" else v)
                        for k, v in p.items()} for p in evidence["probes"]],
        })
        card["pending_owner_replies"] = []
        book.save_card(card)
        book.log({"chat": cid, "event": "turn", "turn": turn, "mode": mode,
                  "route": "qwen3-4b-local" if generated is not None else "",
                  "verdict": name, "text": message, "skipped_reason": reason,
                  "goal": str(generated.get("goal", ""))[:350] if isinstance(generated, dict) else "",
                  "reply_summary": str(generated.get("reply_summary", ""))[:350] if isinstance(generated, dict) else "",
                  "history": history.get("count", 0),
                  "probes": [{"id": p.get("id"), "code": p.get("code")} for p in probes]})
    if reason:
        return {"action": "none", "why": reason, "verdict": name}
    if mode != "send":
        return {"action": "none", "why": f"shadow:{name}", "verdict": name}
    if name in _STOPS and notify_fn:
        notice = (f"Надзор: {str(generated['issue']).strip()}. "
                  f"По ответу агента: {str(generated['message']).strip()} "
                  f"Чат: {card['url']}")
        try:
            notify_fn(_redact_text(notice[:1000]))
        except Exception:  # noqa: BLE001 - ambiguous delivery must not trigger resend
            pass
        book.log({"chat": cid, "event": "owner_notice", "verdict": name,
                  "text": notice[:1000], "sent": True})
    return {"action": "send", "text": message, "verdict": name}
