"""Durable ChatGPT conversation length handoff coordination.

The browser observes UI pressure; only the MCP server persists a handoff and
authorizes a successor. Old and new browser URLs must never identify a worker
or confer rights on their own.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
import json
import collections
import os
import re
import threading
import time
import uuid

CHAT_RE = re.compile(r"^https://chatgpt\.com/(?:g/[^/]+/)?c/([0-9a-fA-F-]{8,})/?(?:[?#].*)?$")
AGENT_RE = re.compile(r"^(?:Q\d+(?:-[A-Z0-9]+)?|W\d+|S-[0-9a-fA-F]{10})(?:\.[2-9]\d*)?$", re.I)
THRESHOLD = 0.85
AUDIT_CAPACITY_CHARS = 3_000_000
AUDIT_RESULT_LIMIT = 12_000
_LOCAL = threading.RLock()
_AUDIT_LOCK = threading.Lock()
_AUDIT_INDEX: dict[str, Any] = {"files": {}, "totals": collections.defaultdict(int), "calls": collections.defaultdict(int),
                                "checked": 0.0}


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _chat_id(url: str) -> str:
    match = CHAT_RE.fullmatch(str(url or ""))
    if not match:
        raise ValueError("handoff requires a real ChatGPT conversation URL")
    return match.group(1).lower()


def _agent(tag: str) -> str:
    tag = str(tag or "").strip()
    if not AGENT_RE.fullmatch(tag):
        raise ValueError("handoff: unsupported agent tag")
    return tag.upper()


def rotated_tag(tag: str) -> str:
    base = _agent(tag)
    match = re.search(r"\.([2-9]\d*)$", base)
    generation = int(match.group(1)) + 1 if match else 2
    return (base[:match.start()] if match else base) + f".{generation}"


def _arg_size(value: Any) -> int:
    if isinstance(value, dict):
        if "sha256" in value and "length" in value:
            return max(0, int(value["length"]))
        return sum(_arg_size(v) for v in value.values())
    if isinstance(value, list):
        return sum(_arg_size(v) for v in value)
    if isinstance(value, str):
        # audit._trim keeps the length of a truncated non-content field.
        match = re.search(r"…\(\+(\d+)\)$", value)
        return len(value) + (int(match.group(1)) if match else 0)
    return 0


def audit_usage(agent: str, *, logs_dir: Path | None = None, now: float | None = None) -> dict[str, Any]:
    """Incremental, redacted audit accounting. Never loads tool payloads into the response.

    Existing audit.result_bytes measures untrimmed output; MCP clips at 12k.
    The length of content args is already recorded in audit as a hash+length.
    """
    tag = _agent(agent)
    current = time.monotonic() if now is None else now
    logs_dir = logs_dir or Path(__file__).resolve().parents[3] / "logs"
    with _AUDIT_LOCK:
        idx = _AUDIT_INDEX
        # One scan per minute across *all* agents, rather than a full scan per tab.
        if current - idx["checked"] >= 60 or idx.get("root") != str(logs_dir):
            if idx.get("root") != str(logs_dir):
                idx.update(root=str(logs_dir), files={}, totals=collections.defaultdict(int),
                           calls=collections.defaultdict(int))
            cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()
            for path in sorted(logs_dir.glob("audit*.jsonl")):
                if path.name != "audit.jsonl" and path.stem[6:16] < cutoff:
                    continue
                filename = str(path)
                try:
                    stat = path.stat()
                    # A daily log rename preserves file identity. Key by inode
                    # so old audit.jsonl is not charged *again* as an archive.
                    identity = (f"{stat.st_dev}:{stat.st_ino}" if stat.st_ino else filename)
                    start = idx["files"].get(identity, 0)
                    if stat.st_size < start:
                        start = 0
                    if stat.st_size == start:
                        continue
                    with path.open("rb") as fh:
                        fh.seek(start)
                        while True:
                            pos = fh.tell()
                            line = fh.readline()
                            if not line:
                                break
                            if not line.endswith(b"\n"):
                                fh.seek(pos)
                                break
                            try:
                                event = json.loads(line)
                                worker = str(event.get("agent") or "").upper()
                                if not AGENT_RE.fullmatch(worker):
                                    continue
                                size = _arg_size(event.get("args") or {})
                                result = min(max(0, int(event.get("result_bytes") or 0)),
                                             AUDIT_RESULT_LIMIT)
                            except (ValueError, TypeError, OverflowError):
                                continue
                            idx["totals"][worker] += size + result
                            idx["calls"][worker] += 1
                        idx["files"][identity] = fh.tell()
                except OSError:
                    continue
            idx["checked"] = current
        chars = idx["totals"].get(tag, 0)
        return {"source": "audit", "chars": chars, "calls": idx["calls"].get(tag, 0),
                "capacity_chars": AUDIT_CAPACITY_CHARS,
                "fill": round(min(1.0, chars / AUDIT_CAPACITY_CHARS), 4)}


class Book:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir) / "handoff"
        self.path = self.dir / "state.json"
        self.lockfile = self.dir / "state.lock"

    @contextmanager
    def locked(self) -> Iterator[dict[str, Any]]:
        self.dir.mkdir(parents=True, exist_ok=True)
        with _LOCAL:
            deadline = time.monotonic() + 8.0
            while True:
                try:
                    fd = os.open(self.lockfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.close(fd)
                    break
                except FileExistsError:
                    try:
                        if time.time() - self.lockfile.stat().st_mtime > 20:
                            self.lockfile.unlink()
                            continue
                    except OSError:
                        pass
                    if time.monotonic() >= deadline:
                        raise TimeoutError("handoff state lock busy")
                    time.sleep(0.05)
            try:
                state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
                if not isinstance(state, dict):
                    raise ValueError("invalid handoff state")
                yield state
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(tmp, self.path)
            finally:
                self.lockfile.unlink(missing_ok=True)

    def entries(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}


def _key(agent: str, url: str) -> str:
    return f"{_agent(agent)}:{_chat_id(url)}"


def _file(book: Book, tag: str, timestamp: str) -> Path:
    return book.dir / f"{tag.replace(':', '-')}-{timestamp}.md"


def _prompt(entry: dict[str, Any]) -> str:
    return ("⟦надзор⟧ Разговор заполнен примерно на 85%. Сохрани точную точку продолжения "
            "через коннектор dev-mcp: вызови fleet_chat_handoff(agent="
            f"'{entry['agent']}', command='save', url='{entry['old_url']}', "
            "content='<Markdown с разделами: Цель; Сделано и подтверждения; Решения; "
            "Отвергнутое; Неизвестное; Следующий шаг; Идущие процессы; Права, scope, "
            "worker и claim_id>'). Не добавляй выдуманных фактов. После успешной записи "
            f"ответь ровно одной строкой: ⟦HANDOFF_READY:{entry['token']}⟧. "
            "Не продолжай исходное задание в этом переполненном чате.")


def observe(book: Book, *, agent: str, url: str, fill: float, chars: int,
            full: bool = False, source: str = "dom") -> dict[str, Any]:
    tag, cid = _agent(agent), _chat_id(url)
    fill = max(0.0, min(10.0, float(fill)))
    chars = max(0, min(50_000_000, int(chars)))
    sizes = {"fill": round(min(1.0, fill), 4), "chars": chars, "source": source}
    if not full and fill < THRESHOLD:
        return {"action": "none", **sizes}
    with book.locked() as data:
        key = f"{tag}:{cid}"
        entry = data.get(key)
        if entry is None:
            token = uuid.uuid4().hex[:12]
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            entry = {"token": token, "agent": tag,
                     "old_url": f"https://chatgpt.com/c/{cid}", "status": "request",
                     "created": _utc(), "file": str(_file(book, tag, timestamp))}
            data[key] = entry
        sample = {"at": _utc(), "chars": chars, "fill": round(fill, 4),
                  "source": source, "full": bool(full)}
        # Bounded measurements: banner observations retained for future calibration.
        measurements = entry.setdefault("measurements", [])
        if not measurements or (full and not measurements[-1].get("full")):
            measurements.append(sample)
            entry["measurements"] = measurements[-20:]
        if entry["status"] == "request" and not full:
            return {"action": "request", "token": entry["token"], "text": _prompt(entry), **sizes}
        return {"action": "recover" if full and entry["status"] == "request"
                else entry["status"], "token": entry["token"], **sizes}


def save(book: Book, *, agent: str, url: str, content: str) -> dict[str, Any]:
    key = _key(agent, url)
    text = str(content or "").strip()
    if not 100 <= len(text) <= 60_000:
        raise ValueError("handoff requires 100..60000 characters of context")
    with book.locked() as data:
        entry = data.get(key)
        if not entry:
            raise ValueError("handoff not requested for this conversation")
        dest = Path(entry["file"])
        if entry["status"] not in ("request", "saved", "prepared", "activated"):
            raise ValueError("handoff state incompatible with save")
        if dest.exists():
            if dest.read_text(encoding="utf-8") != text + "\n":
                raise ValueError("handoff content already saved; will not overwrite")
        else:
            dest.write_text(text + "\n", encoding="utf-8", errors="strict")
        if entry["status"] == "request":
            entry["status"] = "saved"
            entry["saved_at"] = _utc()
        return {"ok": True, "marker": f"⟦HANDOFF_READY:{entry['token']}⟧",
                "file": str(dest)}


def _fallback(book: Book, entry: dict[str, Any], *, context: str) -> None:
    dest = Path(entry["file"])
    if dest.exists():
        return
    context = str(context or "").strip()[:20_000]
    dest.write_text(
        "# Аварийная передача ChatGPT\n\n"
        f"Agent: {entry['agent']}\nПредыдущий чат: {entry['old_url']}\n"
        f"Время: {_utc()}\n\n"
        "Не удалось получить сводку от агента; сведения ниже только из журнала сервера. "
        "В новом чате проверь claim и свежие файлы перед изменениями.\n\n"
        f"{context or 'Журнал недоступен; состояние задачи неизвестно.'}\n", encoding="utf-8")


def prepare(book: Book, *, agent: str, url: str, token: str = "",
            full: bool = False, context: str = "", quick_book: Any = None,
            startup: str = "") -> dict[str, Any]:
    key = _key(agent, url)
    with book.locked() as data:
        entry = data.get(key)
        if not entry:
            raise ValueError("handoff not observed")
        if token and entry["token"] != token:
            raise PermissionError("handoff marker does not match the conversation")
        if entry["status"] == "request" and not full:
            raise ValueError("handoff file not saved")
        if entry["status"] == "request" and full:
            _fallback(book, entry, context=context)
            entry["status"] = "saved"
            entry["emergency"] = True
        if not Path(entry["file"]).is_file():
            raise ValueError("handoff file missing")
        details = Path(entry["file"]).read_text(encoding="utf-8")[:60_000]
        if entry["status"] not in ("prepared", "activated"):
            entry["status"] = "prepared"
            entry["prepared_at"] = _utc()
            entry["new_agent"] = rotated_tag(entry["agent"])
        new_agent = entry.setdefault("new_agent", rotated_tag(entry["agent"]))
        # Return deterministic same successor on duplicate after worker restart.
        result = {"action": "prepared", "token": entry["token"],
                  "source_url": entry["old_url"], "agent": entry["agent"],
                  "new_agent": new_agent,
                  "file": entry["file"], "emergency": bool(entry.get("emergency")),
                  "start": (startup or "") + "\n\n"
                  f"⟦HANDOFF_SUCCESSOR:{entry['token']}⟧\n"
                  "⟦надзор⟧ Новый чат-преемник. Точка продолжения сохранена сервером "
                  f"в {entry['file']} (предыдущий чат {entry['old_url']}). "
                  f"Используй НОВЫЙ agent='{new_agent}' для всех MCP-вызовов! "
                  "Прежний agent будет отозван при активации чата; claim/scope переходят, "
                  "не создавай новый захват.\n\n"
                  + details}
        if quick_book is not None:
            from . import quick
            job = quick.prepare_successor(quick_book, entry["agent"], entry["old_url"],
                                          result["start"], entry["token"])
            if job["tag"] != new_agent:
                raise PermissionError("handoff quick tag mismatch")
        return result


def activated(book: Book, *, agent: str, url: str, successor_url: str,
              token: str) -> dict[str, Any]:
    key = _key(agent, url)
    new_id = _chat_id(successor_url)
    with book.locked() as data:
        entry = data.get(key)
        if not entry or entry["token"] != token or entry["status"] not in ("prepared", "activated"):
            raise PermissionError("handoff successor not prepared")
        if new_id == _chat_id(entry["old_url"]):
            raise ValueError("successor must be a different conversation")
        existing = entry.get("successor_url")
        successor = f"https://chatgpt.com/c/{new_id}"
        if existing and existing != successor:
            raise PermissionError("successor already activated")
        entry["successor_url"] = successor
        entry["status"] = "activated"
        entry["activated_at"] = entry.get("activated_at") or _utc()
        return {"ok": True, "successor_url": successor, "new_agent": entry["new_agent"]}


def register_successor(store: Any, old_agent: str, new_agent: str) -> str:
    """Insert a non-revoked successor *before* its first tool call, no rights revoked yet."""
    old_agent, new_agent = _agent(old_agent), _agent(new_agent)
    if rotated_tag(old_agent) != new_agent:
        raise ValueError("invalid agent generation")
    now = _utc()
    with store.tx() as tx:
        old = tx.one("SELECT id,label,kind,revoked_at FROM workers WHERE id=?", (old_agent,))
        if old is None and old_agent.startswith("S-"):
            tx.change("""INSERT INTO workers(id,label,kind,first_seen,last_seen,revoked_at,revoke_reason)
                         VALUES (?,?,?, ?, ?,NULL,'')""",
                      (old_agent, "overseer-" + old_agent, "supervised", now, now),
                      event={"kind": "handoff_source", "worker_id": old_agent})
            old = {"id": old_agent, "label": "overseer-" + old_agent,
                   "kind": "supervised", "revoked_at": None}
        if old is None:
            raise PermissionError("handoff: source worker not registered")
        new_label = str(old["label"]) + "-handoff-" + new_agent.rsplit(".", 1)[1]
        exists = tx.one("SELECT id FROM workers WHERE id=?", (new_agent,))
        if exists is None:
            tx.change("""INSERT INTO workers(id,label,kind,first_seen,last_seen,revoked_at,revoke_reason)
                         VALUES (?,?,?,?,?,NULL,'')""",
                      (new_agent, new_label, str(old["kind"]), now, now),
                      event={"kind": "handoff_successor", "worker_id": new_agent,
                             "payload": {"source": old_agent}})
        else:
            new_label = str(tx.one("SELECT label FROM workers WHERE id=?", (new_agent,))["label"])
    return new_label


def transfer_rights(store: Any, old_agent: str, new_agent: str) -> None:
    """Atomic SQLite claim transfer and source revocation, idempotent on retries."""
    register_successor(store, old_agent, new_agent)
    old_agent, new_agent = _agent(old_agent), _agent(new_agent)
    now = _utc()
    with store.tx() as tx:
        old = tx.one("SELECT revoked_at,revoke_reason FROM workers WHERE id=?", (old_agent,))
        if old is None:
            raise PermissionError("source worker missing")
        if old["revoked_at"] is not None:
            if str(old["revoke_reason"]) != f"handoff:{new_agent}":
                raise PermissionError("source worker revoked for a different reason")
            return
        tx.change("UPDATE claims SET worker_id=? WHERE worker_id=? AND state='active'",
                  (new_agent, old_agent),
                  event={"kind": "handoff_claim_transfer", "worker_id": new_agent,
                         "payload": {"from": old_agent}})
        tx.change("UPDATE workers SET revoked_at=?, revoke_reason=? WHERE id=? AND revoked_at IS NULL",
                  (now, f"handoff:{new_agent}", old_agent),
                  event={"kind": "handoff_revoke", "worker_id": old_agent,
                         "payload": {"successor": new_agent}})
    # Роль владельца идёт за преемником (20.11.2026): вкладка владельца, переехавшая в новый
    # чат, не должна просить роль заново.
    from .owner import carry_grant
    carry_grant(store, old_agent, new_agent)


def transfer_overseer(book: Any, old_url: str, new_url: str, new_agent: str) -> None:
    """Move supervision in one registry write, without modifying overseer.py."""
    from . import overseer

    old_id, new_id = _chat_id(old_url), _chat_id(new_url)
    with overseer._lock:
        chats = book.chats()
        source = chats.get(old_id)
        if not source:
            raise PermissionError("handoff source is not under supervision")
        existing = chats.get(new_id)
        if existing and existing.get("handoff_from") != old_id:
            raise PermissionError("successor already supervised independently")
        mode = source.get("mode")
        if mode == "off" and existing:
            return
        if mode not in ("shadow", "send"):
            raise PermissionError("source is not actively supervised")
        chats[new_id] = {**source, "url": f"https://chatgpt.com/c/{new_id}",
                         "tag": _agent(new_agent), "handoff_from": old_id,
                         "added": _utc()}
        chats[old_id]["mode"] = "off"
        book.save_chats(chats)
