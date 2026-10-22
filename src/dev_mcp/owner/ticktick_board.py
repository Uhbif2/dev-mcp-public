"""TickTick board «🤖 Флот»: the owner's workplace with the fleet (project _todo/25, 19.10.2026).

The dev-mcp server itself is the only fleet-side TickTick client (official MCP, OAuth —
``ticktick_oauth``). Mapping, one to one:

* list «🤖 Флот» (kanban)          — the workplace;
* section                          — a fleet project (intent projects get a human name);
* NOTE «▶ Сейчас» atop a section   — what goes on now, one terse line per item (no checkbox);
* card                             — an intent; the server never edits an owner-written card;
* CHECKLIST subtask «◆ …»          — a question to the owner, options are checklist items;
* tags ``флот-*`` (parent ``флот``) — state, filterable across projects;
* comments «Флот · …»               — a terse chronicle.

Facts from the 19.10 probe that shape this code: a NOTE cannot be a subtask; every comment carries
the owner's userId (so the server keeps a ledger of what it wrote); the etag is not enforced, but
an update only touches the fields it sends — so the server never sends owner fields.
"""
from __future__ import annotations

from dev_mcp import machine

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

LOG = logging.getLogger(__name__)

BOARD_NAME = "🤖 Флот"
SYSTEM_COLUMN = "◎ Система"
INBOX_COLUMN = "＋ Новое"
NOW_TITLE = "▶ Сейчас"
STATE_PATH = Path(__file__).resolve().parents[3] / "data" / "fleet" / "ticktick-board.json"
TAG_ROOT = "флот"
# The fleet speaks about the work, never in the owner's voice (owner, 19.10: not «ждёт-меня»).
# A card tag is a monitor of the card's own project, not of its section (20.10): «в-работе» only while
# an agent holds its task and called the fleet within LIVE_S.
TAGS = {"wait_owner": "нужно-решение", "running": "в-работе", "queue": "в-очереди",
        "wait_other": "ждёт-условий", "stuck": "застряло", "accept": "на-приёмку", "paused": "на-паузе"}
MAX_SECTIONS = 15
SYNC_INTERVAL_S = 120
APPROVE_PREFIX = "одобрить операционный план"
_BOILERPLATE_REASON = "для этого шага необходимо действие владельца"


class BoardUnavailable(RuntimeError):
    pass


# ----------------------------------------------------------------------------- MCP access

class Api:
    """Plain ``tools/call`` over the official TickTick MCP with the server's OAuth bearer."""

    def __init__(self, transport: Any, credential: Callable[[], str]):
        self.transport = transport
        self.credential = credential
        self._ready = False
        self.requests = 0
        self.request_seconds = 0.0

    def _request(self, *args, **kwargs):
        started = time.monotonic()
        self.requests += 1
        try:
            return self.transport.request(*args, **kwargs)
        finally:
            self.request_seconds += time.monotonic() - started

    def _init(self, bearer: str) -> None:
        if self._ready:
            return
        self._request("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "dev-mcp-board", "version": "1"}}, bearer)
        self._request("notifications/initialized", {}, bearer, notification=True)
        self._ready = True

    def call(self, name: str, arguments: dict) -> Any:
        bearer = self.credential()
        self._init(bearer)
        result = self._request("tools/call", {"name": name, "arguments": arguments}, bearer)
        if result.get("isError"):
            raise BoardUnavailable(f"TickTick {name} refused")
        data = result.get("structuredContent")
        if data is None:
            texts = [c.get("text") for c in result.get("content") or [] if c.get("type") == "text"]
            try:
                data = json.loads(texts[0]) if texts else None
            except ValueError:
                data = None
        if isinstance(data, dict) and set(data) == {"result"}:
            data = data["result"]
        return data


# ----------------------------------------------------------------------------- fleet view

def _short(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return (cut or text[:limit]).rstrip(",.;:—-") + "…"


_ID_LIKE = re.compile(r"(?:request|revalidate|queue-source|draft|case|styk|triage|review|fix):[\w-]+"
                      r"|\bTZ-[\w-]+|\b[0-9a-f]{8,}\b|^стадия\s+\w+$|^(?:fix|review|blocked)\b", re.I)
_MARKDOWN = re.compile(r"[*_`#]+")
_STAGE_CODE = re.compile(r"\b(?:approach_?selected|executable_?(?:goal|plan)|hypotheses_?plan|clean_?start"
                         r"|handed_?over|owner_?approved|rollback_?plan|understood|implemented)\b", re.I)
_PATH = re.compile(r"\"[A-Za-z]:\\[^\"\n]+\"|[A-Za-z]:\\\S+")  # quoted paths may hold spaces


def _human_task(title: str) -> str:
    """A task title the owner can read, or "" — ids, stage codes and bare Fix/Review are noise."""
    text = re.sub(r"^(?:request|review|fix|triage|styk)\s*[:\-]\s*", "", str(title or ""), flags=re.I)
    text = re.sub(r"\bTZ-[\w-]+\s*[—:-]\s*", "", text)
    text = _MARKDOWN.sub("", text).strip()
    if not text or _ID_LIKE.search(text) or _STAGE_CODE.search(text) or len(re.findall(r"[A-Za-zА-Яа-яЁё]{3,}", text)) < 2:
        return ""
    return _short(text, 55)


INTENT_INBOX = machine.INTENT_INBOX
STACK_JSON = machine.STACK_JSON


def _stack_titles() -> dict[str, str]:
    try:
        data = json.loads(STACK_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    titles = {}
    for comp in data.get("components", []):
        title = re.split(r"\s[—(]", str(comp.get("title") or ""), maxsplit=1)[0].strip()
        if comp.get("id") and title:
            titles[str(comp["id"]).casefold()] = title
    return titles


def _tidy(text: str) -> str:
    text = _PATH.sub(lambda m: "«" + Path(m.group(0).strip('"').rstrip("\\")).stem + "»", text)
    text = re.sub(r"^(?:№\s*\d+(?:\s*\+\s*№\s*\d+)*\s*[:.—-]?\s*)", "", text)
    text = re.sub(r"^(?:также|и ещё|ещё)\s+", "", text, flags=re.I)
    return text[:1].upper() + text[1:]


def _plain(text: str) -> str:
    """Agent markdown → a line for a phone: no emphasis marks, commit hashes or paths."""
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)  # [label](url) → label
    text = re.sub(r"[*`]+", "", text)
    text = re.sub(r"\s*\b(?:commit\s+)?[0-9a-f]{12,40}\b", "", text)
    return _tidy(" ".join(text.split())) if text.strip() else ""


_COMMANDISH = re.compile(r"https?://|SHA-?256|\bpy\s+-3\b|\brc=\d|\b[\w-]+\.(?:py|md|json|toml|cmd)\b|\s-{1,2}[a-z][\w-]*|\b\d+/\d+\b")


def _for_owner(text: str, limit: int = 420) -> str:
    """Whole sentences without commands and test counts, cut at a sentence end — never mid-word."""
    plain = _plain(text)
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", plain) if s.strip()]
    kept, size = [], 0
    for sentence in sentences:
        if _COMMANDISH.search(sentence):
            continue
        if kept and not sentence.rstrip().endswith((".", "!", "?", "»", ")")):
            break  # the source was cut mid-sentence: stop at the last whole one
        if size + len(sentence) > limit:
            break
        kept.append(sentence)
        size += len(sentence) + 1
    if kept:
        return " ".join(kept)
    first = sentences[0] if sentences else plain
    return first if len(first) <= limit else first[:limit].rsplit(" ", 1)[0] + "…"


def intent_title(external_id: str | None, root: Path | None = None) -> str:
    """The owner's own wording: «# Интенция №17 — вернуть к работе …» → the part after the dash;
    «…, дословно: <text>» → the first sentence of the text."""
    if not external_id:
        return ""
    try:
        text = ((root or INTENT_INBOX) / Path(external_id).name).read_text(encoding="utf-8")[:4000]
    except OSError:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    head = _MARKDOWN.sub("", lines[0]).strip()
    if "дословно" in head.casefold():
        body = _MARKDOWN.sub("", " ".join(lines[1:])).strip(" «»\"'")
        sentence = re.split(r"(?<=[.!?…])\s", body, maxsplit=1)[0]
        return _short(_tidy(sentence), 70)
    if " — " in head:
        head = head.rsplit(" — ", 1)[1]
    head = re.sub(r"^\d+\s*", "", head).strip()
    return _short(_tidy(head), 70)


def section_of(project: str, active: set[str], titles: dict[str, str],
               intent: str = "") -> tuple[str, str]:
    """(group key, human section name): an intent project lands in its target project's section.

    Order: pult component (exact id, then id prefix — project names are cut at 18 chars) →
    an existing fleet project → «the quoted name» in the owner's intent → its head before «:» →
    the slug, humanised.
    """
    base = project.split("-intent-", 1)[0] if "-intent-" in project else project
    key = base.casefold()
    comp = key if key in titles else next((c for c in sorted(titles) if c.startswith(key) and len(key) >= 6), None)
    if comp:
        return "c:" + comp, titles[comp]
    full = next((p for p in sorted(active) if "-intent-" not in p and p != base and p.startswith(base)), None)
    if full:
        return "p:" + full, full
    if project in active and "-intent-" not in project and not re.fullmatch(r"[a-z0-9-]+", project):
        return "p:" + project, project  # a real fleet project name («Beta App», «Mill Works»)
    quoted = re.search(r"«([^»]{3,40})»", intent)
    if quoted:
        return "p:" + base, quoted.group(1)
    head = intent.split(":", 1)[0].strip()
    if intent and ":" in intent and 3 <= len(head) <= 40 and "," not in head and len(head.split()) <= 5:
        return "p:" + base, head
    human = base.replace("-", " ").strip()
    return "p:" + base, _short(human[:1].upper() + human[1:], 32)


BUCKETS = ("running", "silent", "checking", "ready", "waiting", "stuck")
LIVE_S = 5 * 60          # an agent that called the fleet within 5 min is working right now
STUCK_DAYS = 7           # frozen longer than this is history, not a live jam


def _age_s(stamp: Any, now: datetime) -> float | None:
    try:
        at = datetime.fromisoformat(str(stamp).replace(" ", "T"))
    except (TypeError, ValueError):
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return (now - at).total_seconds()


STUCK_CAUSES = {"platform": "ждёт платформу", "chain": "ждёт другую задачу",
                "resource": "ждёт ресурс/стенд", "own": "по существу"}


def _review_of_submitted(row: Any, states: dict) -> bool:
    """Замороженное ревью сданной задачи: на доске его представляет сама задача."""
    tz = str(row["tz"])
    if not tz.startswith("review:") or row["state"] != "frozen":
        return False
    target = tz[len("review:"):]
    if (row["project"], target) not in states:
        target = re.sub(r":\d+$", "", target)
    return states.get((row["project"], target)) == "submitted"


def _stuck_cause(store: Any, row: Any, states: dict) -> str:
    """Почему застряло — по последней причине блокировки задачи (или её ревью)."""
    project, tz = row["project"], str(row["tz"])
    if row["state"] == "ready":
        return "chain"
    targets = [tz]
    if row["state"] == "submitted":
        targets = [t for (p, t), st in states.items() if p == project and st == "frozen"
                   and (t == "review:" + tz or t.startswith("review:" + tz + ":"))] or [tz]
    with store.read() as tx:
        event = tx.one("SELECT kind,payload FROM events WHERE project=? AND tz=? AND kind IN "
                       "('block','blocker_loop_stopped') ORDER BY id DESC LIMIT 1", (project, targets[-1]))
    if event is None:
        return "own"
    if event["kind"] == "blocker_loop_stopped":
        return "chain"
    try:
        payload = json.loads(event["payload"] or "{}")
    except (TypeError, ValueError):
        payload = {}
    policy = (payload.get("reactivate_policy") or {}).get("kind")
    reason = str(payload.get("reason") or "")
    if policy == "accepted_work" or "project blocker — ждёт" in reason:
        return "chain"
    if policy in ("platform_deploy_after", "manual_platform") or "infrastructure" in reason:
        return "platform"
    if policy in ("world_resource", "resource") or "resources:" in reason or "ресурс" in reason:
        return "resource"
    return "own"


def live_bucket(row: Any, states: dict, seen: dict, now: datetime) -> str | None:
    """Where a task really is, for the owner's monitor.

    running — an agent holds it and called the fleet ≤5 min ago; silent — held, agent quiet;
    checking — handed in and a reviewer is on it or about to be; ready — free to take now;
    waiting — its prerequisites are not done; stuck — frozen (or its review is) within a week."""
    project, tz, state = row["project"], row["tz"], row["state"]
    if state == "taken":
        age = _age_s(seen.get((project, tz)), now)
        return "running" if age is not None and age <= LIVE_S else "silent"
    if state == "submitted":
        reviews = [s for (p, t), s in states.items() if p == project
                   and (t == "review:" + tz or t.startswith("review:" + tz + ":"))]
        if "frozen" in reviews and not {"taken", "ready"} & set(reviews):
            return "stuck"
        return "checking"
    if state == "ready":
        try:
            needs = json.loads(row["needs"] or "[]")
        except (TypeError, ValueError):
            needs = []
        unmet = [states.get((project, str(n).split("#", 1)[0])) for n in needs]
        unmet = [s for s in unmet if s != "accepted"]
        if any(s in (None, "frozen", "rejected") for s in unmet):
            return "stuck"  # waits for a task that is stopped or does not exist: it will never come
        return "waiting" if unmet else "ready"
    age = _age_s(row["updated_at"], now)
    return "stuck" if age is not None and age <= STUCK_DAYS * 86400 else None


def fleet_sections(store: Any, snapshot: dict, pins: dict[str, tuple[str, str]] | None = None,
                   accepted: dict[str, str] | None = None) -> list[dict]:
    """One section per target project, ordered: needs you, running, rest.

    A handed-over intent stays on the board «на-приёмку» until the owner ticks its card;
    `accepted` (project → digest of the result the owner accepted) takes it off."""
    human = snapshot.get("human", {})
    cards = {c["project"]: c for c in human.get("projects", [])}
    done = {d["project"]: d for d in human.get("done", [])
            if d.get("project") and d["project"] not in cards
            and (accepted or {}).get(d["project"]) != _digest(_for_owner(d.get("text") or ""))}
    actions: dict[str, list[dict]] = {}
    for action in human.get("actions", []):
        actions.setdefault(action["project"], []).append(action)
    with store.read() as tx:
        active = {r["name"] for r in tx.query("SELECT name FROM projects WHERE active=1")}
        rows = tx.query("SELECT project,tz,title,state,needs,updated_at FROM tasks "
                        "WHERE state IN ('ready','taken','submitted','frozen')")
        states = {(r["project"], r["tz"]): r["state"] for r in tx.query(
            "SELECT project,tz,state FROM tasks WHERE project IN (SELECT DISTINCT project FROM tasks "
            "WHERE state IN ('ready','taken','submitted','frozen'))")}
        seen = {(r["project"], r["tz"]): r["last_seen"] for r in tx.query(
            "SELECT c.project,c.tz,w.last_seen FROM claims c LEFT JOIN workers w ON w.id=c.worker_id "
            "WHERE c.state='active'")}
        externals = {}
        has_outcomes = tx.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outcomes'")
        for r in [] if not has_outcomes else tx.query("SELECT project,spec FROM outcomes WHERE parent_id IS NULL "
                          "AND state!='dropped' AND spec LIKE '%intent_ref%'"):
            try:
                externals[r["project"]] = json.loads(r["spec"]).get("external_id")
            except (TypeError, ValueError):
                pass
    titles = _stack_titles()
    work: dict[str, dict[str, list[str]]] = {}
    now = datetime.now(timezone.utc)
    causes: dict[str, dict[str, int]] = {}
    for row in rows:
        if _review_of_submitted(row, states):
            continue  # 21.10: сданная задача уже считается «застряло» — её ревью не второй раз
        bucket = live_bucket(row, states, seen, now)
        if bucket:
            work.setdefault(row["project"], {}).setdefault(bucket, []).append(_human_task(row["title"]))
            if bucket == "stuck":
                cause = _stuck_cause(store, row, states)
                bag = causes.setdefault(row["project"], {})
                bag[cause] = bag.get(cause, 0) + 1
    projects = (set(cards) | set(actions) | set(done)
                | {p for p, w in work.items() if w.get("running") or w.get("silent") or w.get("ready")}) & active
    groups: dict[str, dict] = {}
    for project in sorted(projects):
        owner_words = intent_title(externals.get(project))
        key, name = (pins or {}).get(externals.get(project) or "", (None, None))
        if key is None:
            key, name = section_of(project, active, titles, owner_words)
        group = groups.setdefault(key, {"key": key, "name": name, "intents": [], "actions": [],
                                        **{bucket: [] for bucket in BUCKETS}})
        load = work.get(project, {})
        for bucket in BUCKETS:
            group[bucket].extend(load.get(bucket, []))
        for cause, n in causes.get(project, {}).items():
            group.setdefault("stuck_causes", {})[cause] = group.get("stuck_causes", {}).get(cause, 0) + n
        group["actions"].extend(actions.get(project, []))
        if project in cards or project in actions or project in done:
            card = cards.get(project) or {}
            finished = done.get(project)
            with store.read() as tx:
                root_row = tx.one("SELECT root FROM projects WHERE name=?", (project,))
            brief = read_brief(Path(root_row["root"])) if root_row else {}
            group["intents"].append({
                "project": project, "card": card, "actions": actions.get(project, []), "brief": brief,
                "external": externals.get(project),
                "root": root_row["root"] if root_row else None,
                "load": {bucket: len(load.get(bucket, [])) for bucket in BUCKETS},
                "handed_over": finished is not None,
                "done_text": _for_owner((finished or {}).get("text") or ""),
                "title": owner_words or _short(card.get("title") or (finished or {}).get("title")
                                               or name, 70)})
    sections = list(groups.values())
    sections.sort(key=lambda s: (not s["actions"], not s["running"], -len(s["ready"]), s["name"]))
    return sections[:MAX_SECTIONS]


# The intent project's own server-managed checker: the fleet's run refuses absolute paths (20.10).
BRIEF_CHECK = "py -3 .intent/check.py owner_brief"
HELP = """Новое намерение — карточка в «＋ Новое» или в разделе нужного проекта. С телефона: тег «флот» на любой задаче — она переедет сюда.
Уточнить — поправьте текст карточки, напишите комментарий или добавьте подпункт: подпункт станет отдельной задачей флота.
Ответить на вопрос ◆ — отметьте один вариант в его списке.
Принять — галочка на карточке или подпункте, когда на нём тег «на-приёмку»; не так — комментарий.
Остановить — «Не буду делать», галочка раньше времени или удаление; снимете отметку или восстановите — флот продолжит.
Теги: нужно-решение — ждёт вас; в-работе — агент работает прямо сейчас; в-очереди — готово к работе или на проверке, ждёт агента; ждёт-условий — ждёт других задач; застряло — работа остановилась; на-приёмку — проверьте.
«Сейчас» вверху раздела — что происходит в проекте; флот пишет в карточки только комментарии «Флот · …», ваш текст не трогает."""
INTAKE_TAG = "флот"
_TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                     ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p",
                      "r", "s", "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def slug(title: str, limit: int = 40) -> str:
    """ASCII, lower case, dashes — the pult's naming rule for folders and project names."""
    text = "".join(_TRANSLIT.get(ch, ch) for ch in title.casefold())
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return (text[:limit].rstrip("-") or "intent")


def owner_intent_text(title: str, content: str, section: str | None, task_id: str) -> str:
    """The owner's words verbatim; the trailer only says where they came from."""
    body = f"# {title.strip()}\n"
    if content.strip():
        body += "\n" + content.strip() + "\n"
    body += ("\n---\nИсточник: карточка владельца в TickTick («🤖 Флот»"
             + (f", раздел «{section}»" if section else "") + f"), id {task_id}. "
             "Уточнения владельца придут комментариями и подпунктами; ответы и вопросы — на этой карточке.\n")
    return body


def read_brief(root: Path) -> dict[str, str]:
    """``intent/owner-brief.md``: «## Коротко» and «## Вопросы» in plain words (owner, 19.10:
    a card must explain itself to someone who forgot everything)."""
    try:
        text = (root / "intent" / "owner-brief.md").read_text(encoding="utf-8")[:6000]
    except OSError:
        return {}
    found: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip().casefold()
            found[current] = []
        elif current is not None:
            found[current].append(line)
    return {k: "\n".join(v).strip() for k, v in found.items()}


def brief_for_question(brief: dict[str, str], action: str) -> str:
    """The plain explanation of one question: the bullet of «Вопросы» that names it."""
    block = brief.get("вопросы", "")
    key = action.casefold()[:30]
    for bullet in re.split(r"\n(?=\s*[-*] )", block):
        if key and key in bullet.casefold():
            return re.sub(r"^\s*[-*]\s*", "", bullet).strip()
    return ""


def _named(items: list[str], limit: int = 2) -> str:
    good = [i for i in dict.fromkeys(items) if i][:limit]
    return (": " + "; ".join(good)) if good else ""


def now_lines(section: dict, stamp: str) -> str:
    lines = []
    for intent in section["intents"]:
        card = intent["card"]
        stages = card.get("stages") or []
        if stages:
            done = sum(1 for s in stages if s.get("done"))
            lines.append(f"◐ {_short(intent['title'], 40)} — этап {min(done + 1, len(stages))} "
                         f"из {len(stages)}, {str(card.get('current', '')).lower()}")
    if section["actions"]:
        lines.append(f"◆ Нужно решение ({len(section['actions'])})"
                     + _named([_short(a["action"], 50) for a in section["actions"]]))
    if section["running"]:
        lines.append(f"▸ Агент работает ({len(section['running'])})" + _named(section["running"]))
    if section.get("silent"):
        lines.append(f"◌ Взято, агент молчит больше {LIVE_S // 60} мин ({len(section['silent'])})"
                     + _named(section["silent"], 1))
    if section["checking"]:
        lines.append(f"✓ На проверке: {len(section['checking'])}")
    if section["ready"]:
        lines.append(f"⋯ Готово к выдаче, ждёт свободного агента: {len(section['ready'])}")
    if section["waiting"]:
        lines.append(f"‖ Ждёт других задач ({len(section['waiting'])})" + _named(section["waiting"], 1))
    if section.get("stuck"):
        why = section.get("stuck_causes") or {}
        split = ", ".join(f"{STUCK_CAUSES[c]} {why[c]}" for c in STUCK_CAUSES if why.get(c))
        lines.append(f"✕ Застряло за неделю ({len(section['stuck'])}" + (f": {split}" if split else "") + ")"
                     + _named(section["stuck"], 2))
    if not lines:
        lines.append("Тихо — ничего не идёт")
    lines.append(f"— {stamp}")
    return "\n".join(lines)


ACCEPT_HINT = ("Проверьте. Всё так — поставьте галочку на карточке, флот закроет работу. "
               "Не так — напишите комментарий.")


def card_text(intent: dict) -> str:
    if intent.get("handed_over"):
        return "Готово: " + (intent.get("done_text") or "результат передан") + "\n\n" + ACCEPT_HINT
    short = (intent.get("brief") or {}).get("коротко", "")
    if short:
        return short + "\n\nУточнить — комментарием или подпунктом к этой карточке."
    return ("Флот готовит короткое объяснение простыми словами — появится здесь.\n\n"
            "Уточнить — комментарием или подпунктом к этой карточке.")


def now_title(section: dict) -> str:
    """One readable line, visible on the board without opening the note."""
    parts = []
    stages = [i["card"].get("stages") or [] for i in section["intents"] if i["card"].get("stages")]
    if len(stages) == 1:
        done = sum(1 for s in stages[0] if s.get("done"))
        parts.append(f"этап {min(done + 1, len(stages[0]))} из {len(stages[0])}")
    if section["actions"]:
        parts.append("нужно ваше решение")
    if section["running"]:
        parts.append(f"работает агентов: {len(section['running'])}")
    if section.get("silent"):
        parts.append(f"{len(section['silent'])} без ответа агента")
    if section["checking"]:
        parts.append(f"{len(section['checking'])} на проверке")
    if section["ready"]:
        parts.append(f"{len(section['ready'])} в очереди")
    if section["waiting"] and not section["running"]:
        parts.append(f"{len(section['waiting'])} ждут других задач")
    if section.get("stuck"):
        parts.append(f"{len(section['stuck'])} застряло")
    return "Сейчас: " + (", ".join(parts) if parts else "тихо")


def state_tag(intent: dict, section: dict, *, paused: bool = False) -> str:
    if paused:
        return TAGS["paused"]
    if intent["actions"]:
        return TAGS["wait_owner"]
    if intent.get("handed_over"):
        return TAGS["accept"]
    load = intent.get("load") or {}
    if load.get("running"):
        return TAGS["running"]
    if load.get("ready") or load.get("checking"):
        return TAGS["queue"]
    if load.get("stuck") or load.get("silent"):
        return TAGS["stuck"]
    return TAGS["wait_other"]


def question_body(action: dict) -> tuple[str, str, list[str]]:
    reason = " ".join(str(action.get("reason") or "").split())
    if not reason or reason.casefold().strip(" .") == _BOILERPLATE_REASON:
        reason = "агент не объяснил — флот переспросит"
    default = " ".join(str(action.get("default") or "").split()) or "без вас не делаем"
    title = "◆ " + _short(action["action"], 150)
    desc = f"Почему вы: {reason}\nЕсли молчите: {default}"
    options = (["Да, выполняйте", "Нет"] if action["action"].casefold().startswith("одобрить")
               else ["Да / сделано", "Нет"])
    return title, desc, options


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


# ----------------------------------------------------------------------------- the board

class Board:
    def __init__(self, store: Any, api: Api, *, state_path: Path = STATE_PATH,
                 clock: Callable[[], datetime] = lambda: datetime.now().astimezone()):
        self.store = store
        self.api = api
        self.state_path = state_path
        self.clock = clock
        self.state = self._load()
        self._board: list[dict] | None = None

    # -- state -----------------------------------------------------------------------------
    def _load(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {"list_id": None, "columns": {}, "notes": {}, "cards": {}, "questions": {},
                "hashes": {}, "ledger": [], "tags": []}

    def _save(self) -> None:
        self.state["ledger"] = self.state["ledger"][-2000:]
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _wrote(self, kind: str, object_id: str) -> None:
        self.state["ledger"].append({"kind": kind, "id": object_id, "at": time.time()})
        self._save()  # a network failure later in the cycle must not orphan this object

    # -- structure -------------------------------------------------------------------------
    def _ensure_list(self) -> str:
        if self.state.get("list_id"):
            return self.state["list_id"]
        lists = self.api.call("list_projects", {}) or []
        found = next((p for p in lists if p.get("name") == BOARD_NAME and not p.get("closed")), None)
        if found is None:
            found = self.api.call("create_project", {"name": BOARD_NAME, "view_mode": "kanban"})
            self._wrote("project", found["id"])
        self.state["list_id"] = found["id"]
        return found["id"]

    def _ensure_column(self, key: str, name: str, order: int, spare: list[str] | None = None) -> str:
        columns = self.state["columns"]
        names = self.state.setdefault("column_names", {})
        if key in columns:
            column_id = columns[key]
            if names.get(column_id) != [name, order]:
                self.api.call("update_column", {"project_id": self.state["list_id"], "column_id": column_id,
                                                "column": {"id": column_id, "name": name, "sortOrder": order}})
                names[column_id] = [name, order]
            return column_id
        if spare:
            # Reuse a section whose project left the board: TickTick has no column deletion.
            column_id = spare.pop(0)
            self.api.call("update_column", {"project_id": self.state["list_id"], "column_id": column_id,
                                            "column": {"id": column_id, "name": name, "sortOrder": order}})
            names[column_id] = [name, order]
            columns[key] = column_id
            return column_id
        existing = {c.get("name"): c.get("id") for c in
                    (self.api.call("list_columns", {"project_id": self.state["list_id"]}) or [])}
        column_id = existing.get(name)
        if column_id is None:
            column = self.api.call("create_column", {"project_id": self.state["list_id"],
                                                     "column": {"name": name, "sortOrder": order}})
            column_id = column["id"]
            self._wrote("column", column_id)
            names[column_id] = [name, order]
        columns[key] = column_id
        return column_id

    def _ensure_tags(self) -> None:
        if self.state.get("tags") == sorted(TAGS.values()):
            return
        known = {t.get("name") for t in (self.api.call("list_tags", {}) or [])}
        if TAG_ROOT not in known:
            self.api.call("create_tag", {"tag": {"name": TAG_ROOT, "color": "#8C5EFA"}})
        for name in TAGS.values():
            if name not in known:
                self.api.call("create_tag", {"tag": {"name": name, "parent": TAG_ROOT}})
        self.state["tags"] = sorted(TAGS.values())
        self.state["duration_tags"] = sorted(name for name in known
                                             if re.fullmatch(r"\d+-(?:минут|час|часа|часов)", name))

    def _upsert_note(self, key: str, column: str, title: str, content: str, order: int) -> None:
        note_id = self.state["notes"].get(key)
        digest = _digest([title, content])
        if note_id and self.state["hashes"].get(note_id) == digest:
            return
        task = {"projectId": self.state["list_id"], "columnId": column, "kind": "NOTE",
                "title": title, "content": content, "sortOrder": order}
        if note_id:
            try:
                self.api.call("update_task", {"task_id": note_id, "task": {"id": note_id, **task}})
            except BoardUnavailable:
                note_id = None  # deleted by the owner: a display, so it is recreated
        if not note_id:
            note_id = self.api.call("create_task", {"task": task})["id"]
            self._wrote("note", note_id)
            self.state["notes"][key] = note_id
        self.state["hashes"][note_id] = digest

    def _merge_tags(self, task_id: str, wanted: str) -> None:
        if self.state["hashes"].get("tag:" + task_id) == wanted:
            return
        current = self.api.call("get_task_by_id", {"task_id": task_id}) or {}
        mine = set(TAGS.values())
        tags = [t for t in current.get("tags") or [] if t not in mine] + [wanted]
        self.api.call("update_task", {"task_id": task_id, "task": {
            "id": task_id, "projectId": self.state["list_id"], "tags": tags}})
        self.state["hashes"]["tag:" + task_id] = wanted

    def _ensure_card(self, intent: dict, column: str) -> str:
        key = intent["project"]
        homes = self.state.setdefault("card_columns", {})
        owned = next((tid for tid, rec in self.state.setdefault("owner_cards", {}).items()
                      if rec.get("file") and rec["file"] == intent.get("external")), None)
        if owned:
            self.state["cards"][key] = owned
            rec = self.state["owner_cards"][owned]
            if (rec.get("column") == self.state["columns"].get("inbox") and column != rec.get("column")
                    and rec.get("why") != "inbox"):
                # From «＋ Новое» to its project's section, once; elsewhere the owner's placement stands.
                self.api.call("update_task", {"task_id": owned, "task": {
                    "id": owned, "projectId": self.state["list_id"], "columnId": column}})
                rec["column"] = column
                self._comment(owned, f"Флот · {self._stamp()} · перенёс в раздел проекта")
            return owned
        card_id = self.state["cards"].get(key)
        if card_id:
            title = _short(intent["title"], 120)
            change: dict[str, Any] = {}
            if homes.get(card_id) != column:
                change["columnId"] = column
            # Only a card the server created itself may be retitled; an owner card never is.
            own = any(e["id"] == card_id and e["kind"] == "card" for e in self.state["ledger"])
            if own and self.state["hashes"].get("title:" + card_id) != title:
                change["title"] = title
            about = card_text(intent)
            if own and self.state["hashes"].get("about:" + card_id) != _digest(about):
                change["content"] = about
                self.state["hashes"]["about:" + card_id] = _digest(about)
            if change:
                self.api.call("update_task", {"task_id": card_id, "task": {
                    "id": card_id, "projectId": self.state["list_id"], **change}})
                homes[card_id] = column
                self.state["hashes"]["title:" + card_id] = title
            return card_id
        task = self.api.call("create_task", {"task": {
            "projectId": self.state["list_id"], "columnId": column, "kind": "TEXT",
            "title": _short(intent["title"], 120),
            "content": card_text(intent)}})
        self.state["hashes"]["about:" + task["id"]] = _digest(card_text(intent))
        self._wrote("card", task["id"])
        self.state["cards"][key] = task["id"]
        homes[task["id"]] = column
        self.state["hashes"]["title:" + task["id"]] = _short(intent["title"], 120)
        return task["id"]

    def _ensure_question(self, card_id: str, action: dict, brief: dict | None = None) -> None:
        qkey = action["project"] + ":" + action["action_id"]
        title, desc, options = question_body(action)
        plain = brief_for_question(brief or {}, action["action"])
        if plain:
            desc = plain + "\n\n" + desc
        plan_sha = self._plan_sha(action["project"], action["action"])
        schedule: dict = {}
        try:
            due = datetime.fromisoformat(str(action.get("deadline") or "").replace("Z", "+00:00"))
            if due.tzinfo is not None:
                schedule = {"dueDate": due.astimezone(timezone(timedelta(hours=3))).isoformat(),
                            "timeZone": "Europe/Moscow", "isAllDay": False}
        except ValueError:
            pass
        duration = action.get("duration_tag")
        tags = [TAGS["wait_owner"]] + ([duration] if duration in self.state.get("duration_tags", []) else [])
        known = self.state["questions"].get(qkey)
        if known:
            digest = _digest([title, desc, plan_sha, schedule, tags])
            if known.get("decided") is None and known.get("hash") != digest:
                change = {"id": known["id"], "projectId": self.state["list_id"], "title": title, "desc": desc}
                if known.get("plan_sha256") != plan_sha:
                    current = self.api.call("get_task_by_id", {"task_id": known["id"]}) or {}
                    change["items"] = [dict(item, status=0) for item in current.get("items", [])]
                if known.get("schedule") != schedule:
                    change.update(schedule)
                    if known.get("schedule") and not schedule:
                        change["dueDate"] = "1970-01-01T00:00:00.000+0000"
                if known.get("service_tags") != tags:
                    current = self.api.call("get_task_by_id", {"task_id": known["id"]}) or {}
                    change["tags"] = [t for t in current.get("tags", [])
                                      if t not in known.get("service_tags", [TAGS["wait_owner"]])] + tags
                self.api.call("update_task", {"task_id": known["id"], "task": {
                    **change}})
                known["hash"] = digest
                known["plan_sha256"] = plan_sha
                known.update(schedule=schedule, service_tags=tags)
            return
        task = self.api.call("create_task", {"task": {
            "projectId": self.state["list_id"], "parentId": card_id, "kind": "CHECKLIST",
            "title": title, "desc": desc, "tags": tags, **schedule,
            "items": [{"title": o, "status": 0, "sortOrder": i} for i, o in enumerate(options)]}})
        self._wrote("question", task["id"])
        self.state["questions"][qkey] = {"id": task["id"], "card": card_id, "options": options,
                                        "action": action["action"], "project": action["project"],
                                        "decided": None, "hash": _digest([title, desc, plan_sha, schedule, tags]),
                                        "plan_sha256": plan_sha, "schedule": schedule, "service_tags": tags}

    def _plan_sha(self, project: str, action: str) -> str | None:
        if not action.casefold().startswith(APPROVE_PREFIX):
            return None
        with self.store.read() as tx:
            row = tx.one("SELECT root FROM projects WHERE name=?", (project,))
        plan = Path(row["root"]) / "intent/rollback-plan.md" if row else None
        return hashlib.sha256(plan.read_bytes()).hexdigest() if plan and plan.is_file() else None

    def _ensure_brief(self, intent: dict) -> None:
        brief = intent.get("brief") or {}
        missing = [a["action"] for a in intent["actions"] if not brief_for_question(brief, a["action"])]
        if brief.get("коротко") and not missing:
            return
        if not intent.get("root"):
            return
        key = _digest([intent["project"], bool(brief.get("коротко")), sorted(missing)])
        asked = self.state.setdefault("brief_requests", {})
        if asked.get(intent["project"]) == key:
            return
        from dev_mcp.fleet import review
        target = Path(intent["root"]) / "intent" / "owner-brief.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        questions = "".join(f"\n- {q}" for q in missing) or "\n(открытых вопросов нет — напиши «нет»)"
        tz = review.open_request(
            self.store, "brief-" + key[:8], kind="other", to="queue", project=intent["project"],
            path=str(target), summary="Объяснить владельцу эту интенцию простыми словами",
            detail=("Владелец читает карточку в TickTick через недели, ничего не помня. Напиши "
                    "intent/owner-brief.md:\n## Коротко — 2–4 предложения: что это, зачем владельцу и что он "
                    "получит в итоге. Как другу, без терминов, путей, имён файлов, номеров задач и английского "
                    "жаргона.\n## Вопросы — на каждый открытый вопрос пункт «- <вопрос дословно>: что это "
                    "значит в жизни; если «Да» — …; если «Нет» — …». Открытые вопросы:" + questions +
                    "\nСверься с intent/understood.md и планом, ничего не выдумывай."))
        if tz:
            with self.store.tx() as tx:
                tx.change("UPDATE tasks SET acceptance=? WHERE project=? AND tz=?",
                          (json.dumps([BRIEF_CHECK]), intent["project"], tz),
                          event={"kind": "owner_brief_requested", "project": intent["project"], "tz": tz,
                                 "payload": {"missing": missing}})
        asked[intent["project"]] = key
        self._save()

    # -- owner cards -----------------------------------------------------------------------
    def read_owner_cards(self, board: list[dict]) -> int:
        """Owner cards on the board (and tasks tagged «флот» anywhere) become intents.

        The intent file goes to the owner's intent inbox (_todo/intents), the existing pipeline
        takes it from there. Edits rewrite the file (→ a new revision); closing, «won't do» or
        deleting the card pauses the work, reopening resumes it.
        """
        from dev_mcp.fleet.controller import INTENT_INBOX
        mine = {entry["id"] for entry in self.state["ledger"]}
        cards = self.state.setdefault("owner_cards", {})
        items = self.state.setdefault("owner_items", {})
        server_cards = set(self.state["cards"].values())
        column_names = {cid: name for key, cid in self.state["columns"].items()
                        for name in [self.state.get("column_names", {}).get(cid, [None])[0]]}
        inbox = self.state["columns"].get("inbox")
        made = 0
        # Tagged anywhere → onto the board, into «＋ Новое».
        try:
            tagged = self.api.call("filter_tasks", {"filter": {"tag": [INTAKE_TAG], "status": [0]}}) or []
        except BoardUnavailable:
            tagged = []
        for task in tagged:
            # A parent-tag filter may also return the child state tags: only «флот» itself is intake.
            if (task.get("projectId") == self.state["list_id"] or task.get("parentId")
                    or INTAKE_TAG not in (task.get("tags") or [])):
                continue
            self.api.call("move_task", {"moves": [{"taskId": task["id"], "fromProjectId": task["projectId"],
                                                   "toProjectId": self.state["list_id"]}]})
            self.api.call("update_task", {"task_id": task["id"], "task": {
                "id": task["id"], "projectId": self.state["list_id"], "columnId": inbox}})
            self._comment(task["id"], f"Флот · {self._stamp()} · взял с тега «флот»")
            task = dict(task, projectId=self.state["list_id"], columnId=inbox)
            board.append(task)
        # The official filter can return closed tasks unless status is explicit.
        # Never treat a closed card as a live card (or immediately resume it).
        board = [task for task in board if task.get("status", 0) == 0]
        live = {task["id"]: task for task in board}
        for task in board:
            tid = task["id"]
            if (tid in mine or tid in server_cards or tid in items or task.get("parentId")
                    or task.get("kind") == "NOTE"):
                continue
            title = " ".join(str(task.get("title") or "").split())
            if not title:
                continue
            content = str(task.get("content") or task.get("desc") or "")
            column = task.get("columnId")
            section = None if column in (inbox, self.state["columns"].get("system")) else column_names.get(column)
            digest = _digest([title, content])
            rec = cards.get(tid)
            if rec is None:
                name = slug(title)
                existing = {r.get("file") for r in cards.values()}
                file, n = f"{name}.md", 2
                while file in existing or (INTENT_INBOX / file).exists():
                    file, n = f"{name}-{n}.md", n + 1
                rec = cards[tid] = {"file": file, "column": column, "section": section,
                                    "hash": None, "state": "open"}
                self._comment(tid, f"Флот · {self._stamp()} · принял, разбираюсь — объясню, как понял")
                made += 1
            elif column != rec.get("column"):
                # A new owner placement is a revision request, never direct
                # authority to move already claimed files to another project.
                rec.update(column=column, section=section)
                rec["hash"] = None
                if column == inbox:
                    self._pause(tid, rec, "inbox")
                self._comment(tid, f"Флот · {self._stamp()} · вижу перенос в другой раздел, учитываю в намерении")
            else:
                # Renaming a section changes presentation, not the binding.
                section = rec.get("section")
            if rec["hash"] != digest:
                (INTENT_INBOX / rec["file"]).write_text(owner_intent_text(title, content, section, tid),
                                                        encoding="utf-8")
                if rec["hash"] is not None:
                    self._comment(tid, f"Флот · {self._stamp()} · вижу правку, учитываю")
                rec["hash"] = digest
            if rec["state"] == "paused" and not (rec.get("why") == "inbox" and column == inbox):
                self._resume(tid, rec)
            self._save()
        for tid, rec in cards.items():
            if rec["state"] != "open" or tid in live:
                continue
            try:
                task = self.api.call("get_task_by_id", {"task_id": tid}) or {}
            except BoardUnavailable:
                task = {}
            status = task.get("status")
            if status == 2 and rec.get("handed_over"):
                project = self._project_of(rec)
                rec["state"] = "accepted"
                if project:
                    self._accept(tid, project, rec.get("done_text") or "")
            elif status == 2:
                self._pause(tid, rec, "closed")
            elif status == -1:
                self._pause(tid, rec, "wont_do")
            elif not task:
                rec["missing"] = rec.get("missing", 0) + 1
                if rec["missing"] >= 2:  # one failed read is not a deletion
                    self._pause(tid, rec, "deleted")
        return made

    def _project_of(self, rec: dict) -> str | None:
        if rec.get("project"):
            return rec["project"]
        with self.store.read() as tx:
            if not tx.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outcomes'"):
                return None
            row = tx.one("SELECT project FROM outcomes WHERE parent_id IS NULL AND spec LIKE ?",
                         ('%"external_id":"' + rec["file"] + '"%',))
        return row["project"] if row else None

    def _pause(self, tid: str, rec: dict, why: str) -> None:
        project = self._project_of(rec)
        frozen: list[str] = []
        if project and why != "accepted":
            with self.store.tx() as tx:
                rows = tx.query("SELECT tz FROM tasks WHERE project=? AND state='ready'", (project,))
                frozen = [r["tz"] for r in rows]
                for tz in frozen:
                    tx.change("UPDATE tasks SET state='frozen' WHERE project=? AND tz=? AND state='ready'",
                              (project, tz), event={"kind": "owner.ticktick_paused", "project": project,
                                                    "tz": tz, "payload": {"why": why}})
        with self.store.tx() as tx:
            tx.record_event(kind="owner.ticktick_card", project=project,
                            payload={"why": why, "card": tid, "file": rec["file"]})
        rec.update(state="paused", why=why, frozen=frozen, missing=0)
        if why in ("closed", "wont_do"):
            self._comment(tid, f"Флот · {self._stamp()} · остановил и сохранил; вернуть — снимите отметку")
        self._save()

    def _resume(self, tid: str, rec: dict) -> None:
        project = self._project_of(rec)
        if project and rec.get("frozen"):
            with self.store.tx() as tx:
                for tz in rec["frozen"]:
                    tx.change("UPDATE tasks SET state='ready' WHERE project=? AND tz=? AND state='frozen'",
                              (project, tz), event={"kind": "owner.ticktick_resumed", "project": project,
                                                    "tz": tz, "payload": {"card": tid}})
        rec.update(state="open", frozen=[], why=None)
        self._comment(tid, f"Флот · {self._stamp()} · продолжаю")

    def read_card_ticks(self, intents: dict[str, dict]) -> int:
        """The owner's tick on a card the fleet made: on «на-приёмку» it accepts the result,
        earlier it stops the work (like «Не буду делать»); unticking resumes."""
        live = {task["id"] for task in self._board or [] if task.get("status", 0) == 0}
        owner = self.state.get("owner_cards", {})
        states = self.state.setdefault("card_states", {})
        accepted = 0
        for project, card_id in list(self.state["cards"].items()):
            if card_id in owner:
                continue  # the owner's own cards: read_owner_cards
            rec = states.setdefault(card_id, {"project": project, "file": project, "state": "open"})
            if card_id in live:
                if rec["state"] == "paused":
                    self._resume(card_id, rec)
                    self._save()
                continue
            try:
                task = self.api.call("get_task_by_id", {"task_id": card_id}) or {}
            except BoardUnavailable:
                continue
            status, intent = task.get("status"), intents.get(project) or {}
            if status == 2 and intent.get("handed_over"):
                self._accept(card_id, project, intent.get("done_text") or "")
                states.pop(card_id, None)
                accepted += 1
            elif status in (2, -1) and rec["state"] == "open":
                self._pause(card_id, rec, "closed" if status == 2 else "wont_do")
                self._merge_tags(card_id, TAGS["paused"])
        return accepted

    def _accept(self, card_id: str, project: str, text: str) -> None:
        with self.store.tx() as tx:
            tx.record_event(kind="owner.ticktick_accepted", project=project,
                            payload={"card": card_id, "result": text[:480]})
        self.state.setdefault("accepted", {})[project] = _digest(text)
        if self.state["cards"].get(project) == card_id:
            del self.state["cards"][project]
        self._comment(card_id, f"Флот · {self._stamp()} · принято вами — работа закрыта")
        self._save()

    def read_comments(self) -> int:
        """The owner's comments on intent cards reach the agent of the current stage verbatim."""
        mine = {entry["id"] for entry in self.state["ledger"]}
        seen = self.state.setdefault("seen_comments", {})
        delivered = 0
        for project, card_id in list(self.state["cards"].items()):
            try:
                comments = self.api.call("get_comment", {"project_id": self.state["list_id"],
                                                         "task_id": card_id}) or []
            except BoardUnavailable:
                continue
            known = set(seen.get(card_id, []))
            for comment in comments:
                cid = comment.get("id")
                if not cid or cid in known or cid in mine:
                    continue
                known.add(cid)
                text = " ".join(str(comment.get("title") or "").split())
                if text and not text.startswith("Флот ·"):
                    self._deliver_note(project, text)
                    delivered += 1
            seen[card_id] = sorted(known)[-500:]
        if delivered:
            self._save()
        return delivered

    def _deliver_note(self, project: str, text: str) -> None:
        import uuid
        with self.store.read() as tx:
            row = tx.one("SELECT root FROM projects WHERE name=?", (project,))
            holders = tx.query("SELECT DISTINCT tz FROM claims WHERE project=? AND state='active'", (project,))
        if row:
            notes = Path(row["root"]) / "intent" / "owner-notes.md"
            notes.parent.mkdir(parents=True, exist_ok=True)
            with notes.open("a", encoding="utf-8") as stream:
                stream.write(f"\n- {self.clock().strftime('%d.%m.%Y %H:%M')} (комментарий в TickTick): {text}\n")
        stamp = datetime.now(timezone.utc).isoformat()
        with self.store.tx() as tx:
            for holder in holders:
                tx.change("INSERT INTO messages(id,dedup_key,to_worker,to_task,broadcast,from_kind,from_id,"
                          "kind,body,created_at,delivered_at,acked_at) VALUES(?,'',NULL,?,0,'owner','',"
                          "'note',?,?,NULL,NULL)",
                          ("m-" + uuid.uuid4().hex[:12], f"{project}/{holder['tz']}",
                           "Владелец написал в TickTick: " + text, stamp),
                          event={"kind": "owner.ticktick_comment", "project": project,
                                 "payload": {"chars": len(text)}})

    # -- owner items ---------------------------------------------------------------------
    def read_owner_items(self, sections: list[dict]) -> int:
        """A subtask the owner adds under an intent card is a new requirement: the fleet gets a
        request in that intent's project (first an executable TZ, then the work); the subtask
        carries the state. Ticking or deleting it before the fleet is done withdraws the request."""
        cards = {card_id: project for project, card_id in self.state["cards"].items()}
        mine = {entry["id"] for entry in self.state["ledger"]}
        items = self.state.setdefault("owner_items", {})
        board = self._board if self._board is not None else (
            self.api.call("filter_tasks", {"filter": {
                "projectIds": [self.state["list_id"]], "status": [0]}}) or [])
        board = [task for task in board if task.get("status", 0) == 0]
        live = {task["id"]: task for task in board}
        made = 0
        for task in board:
            if task["id"] in mine or task["id"] in items or task.get("parentId") not in cards:
                continue
            title = " ".join(str(task.get("title") or "").split())
            if not title:
                continue
            project = cards[task["parentId"]]
            tz = self._open_owner_request(project, title, str(task.get("content") or ""))
            items[task["id"]] = {"project": project, "title": title, "tz": tz, "state": "open",
                                 "card": task["parentId"]}
            self._save()
            if tz:
                self._merge_tags(task["id"], TAGS["running"])
                self._comment(task["id"], f"Флот · {self._stamp()} · принято: сначала ТЗ, потом работа")
            made += 1
        for task_id, item in items.items():
            if item["state"] == "open" and task_id in live and item.get("tz"):
                with self.store.read() as tx:
                    row = tx.one("SELECT state FROM tasks WHERE project=? AND tz=?", (item["project"], item["tz"]))
                if row and row["state"] == "accepted" and not item.get("reported"):
                    self._comment(task_id, f"Флот · {self._stamp()} · оформлено задачей флота и принято "
                                           "в работу; готовое отмечу тегом «на-приёмку»")
                    item["reported"] = True
                if row and row["state"] == "accepted" and not item.get("review"):
                    self._item_result(task_id, item)
            if item["state"] != "open" or task_id in live:
                continue
            try:
                gone = self.api.call("get_task_by_id", {"task_id": task_id})
            except BoardUnavailable:
                gone = None
            status = (gone or {}).get("status")
            if status == 2 and item.get("review"):
                with self.store.tx() as tx:
                    tx.record_event(kind="owner.ticktick_item_accepted", project=item["project"],
                                    tz=item["tz"], payload={"title": item["title"], "work": item.get("work")})
                item["state"] = "accepted"
                continue
            self._withdraw(item, "отмечено вами" if status == 2 else "удалено вами")
            item["state"] = "withdrawn"
        return made

    def _item_result(self, task_id: str, item: dict) -> None:
        """The request only drafts TZs; the item is ready when the work they became is done.

        The work = TZ drafts registered in the project while the request ran (until 10 min after
        its acceptance: registration is a separate tick). No new drafts — the agent found the
        requirement already covered by the plan; that is a result to check, too."""
        with self.store.read() as tx:
            request = tx.one("SELECT created_at,updated_at FROM tasks WHERE project=? AND tz=?",
                             (item["project"], item["tz"]))
        if request is None:
            return
        try:
            start = datetime.fromisoformat(request["created_at"])
            settled = datetime.fromisoformat(request["updated_at"]) + timedelta(minutes=10)
        except (TypeError, ValueError):
            return
        if item.get("work") is None:
            if self.clock() < settled:
                return
            with self.store.read() as tx:
                rows = tx.query("SELECT tz,ts FROM events WHERE kind='draft_registered' AND project=?",
                                (item["project"],))
            work = []
            for r in rows:
                try:
                    at = datetime.fromisoformat(r["ts"])
                except (TypeError, ValueError):
                    continue
                if start <= at <= settled and r["tz"] not in work:
                    work.append(r["tz"])
            item["work"] = work
            self._save()
        with self.store.read() as tx:
            states = {r["tz"]: (r["state"], r["title"]) for r in tx.query(
                "SELECT tz,state,title FROM tasks WHERE project=?", (item["project"],))
                if r["tz"] in item["work"]}
        if any(state in ("draft", "ready", "taken", "submitted") for state, _ in states.values()):
            return
        done = [_human_task(title) for state, title in states.values() if state == "accepted"]
        what = ("сделано: " + "; ".join(list(dict.fromkeys(done))[:3])) if done else "флот счёл, что это уже входит в план"
        self._merge_tags(task_id, TAGS["accept"])
        self._comment(task_id, f"Флот · {self._stamp()} · {what}. Всё так — отметьте подпункт; "
                               "не так — напишите комментарий.")
        item["review"] = True
        self._save()

    def _open_owner_request(self, project: str, title: str, content: str) -> str | None:
        from dev_mcp.fleet import review
        with self.store.read() as tx:
            row = tx.one("SELECT root FROM projects WHERE name=? AND active=1", (project,))
        if row is None:
            return None
        root = Path(row["root"])
        notes = root / "intent" / "owner-notes.md"
        notes.parent.mkdir(parents=True, exist_ok=True)
        with notes.open("a", encoding="utf-8") as stream:
            stream.write(f"\n- {self.clock().strftime('%d.%m.%Y %H:%M')} (TickTick): {title}"
                         + (f" — {content.strip()}" if content.strip() else "") + "\n")
        drafts = root / "ToDo" / "tz-drafts"
        drafts.mkdir(parents=True, exist_ok=True)
        ident = "own-" + hashlib.sha256((project + title).encode()).hexdigest()[:8]
        return review.open_request(
            self.store, ident, kind="other", to="queue", project=project, path=str(drafts),
            summary="Владелец добавил к интенции: " + _short(title, 140),
            detail=("Владелец дописал в TickTick подпункт к этой интенции (дословно): «" + title + "»"
                    + (f" — {content.strip()}" if content.strip() else "") + ". Это требование владельца, "
                    "а не предложение. Оформи его исполняемым ТЗ: черновик ToDo/tz-drafts/TZ-<n>-<slug>.md "
                    "с yaml-шапкой (tz, owns, needs, acceptance) — сервер сам зарегистрирует его. Если "
                    "требование уже покрыто текущим планом — так и напиши в черновике со ссылкой. "
                    "Записано в intent/owner-notes.md."))

    def _withdraw(self, item: dict, why: str) -> None:
        if not item.get("tz"):
            return
        with self.store.tx() as tx:
            tx.change("UPDATE tasks SET state='frozen' WHERE project=? AND tz=? "
                      "AND state IN ('draft','ready')", (item["project"], item["tz"]),
                      event={"kind": "owner.ticktick_withdrawn", "project": item["project"],
                             "tz": item["tz"], "payload": {"why": why, "title": item["title"]}})

    # -- decisions -------------------------------------------------------------------------
    def read_decisions(self) -> int:
        decided = 0
        for qkey, q in self.state["questions"].items():
            if q.get("decided") is not None:
                continue
            task = self.api.call("get_task_by_id", {"task_id": q["id"]})
            if not task:
                continue
            ticked = [i for i, item in enumerate(task.get("items") or []) if item.get("status") == 1]
            if len(ticked) > 1:
                if not q.get("warned"):
                    self._comment(q["card"], "Флот · отмечено несколько вариантов — оставьте один")
                    q["warned"] = True
                continue
            if len(ticked) != 1:
                continue
            option = q["options"][ticked[0]] if ticked[0] < len(q["options"]) else str(ticked[0])
            if not self._record_decision(q, option, approve=(ticked[0] == 0)):
                self.api.call("update_task", {"task_id": q["id"], "task": {
                    "id": q["id"], "projectId": self.state["list_id"],
                    "items": [dict(item, status=0) for item in task.get("items", [])]}})
                self._comment(q["card"], "Флот · план изменился после вопроса — обновляю его; выберите вариант заново")
                continue
            q["decided"] = option
            self.api.call("update_task", {"task_id": q["id"], "task": {
                "id": q["id"], "projectId": self.state["list_id"], "status": 2, "tags": []}})
            self._comment(q["card"], f"Флот · {self._stamp()} · решено: {option}")
            decided += 1
        return decided

    def _record_decision(self, q: dict, option: str, *, approve: bool) -> bool:
        payload: dict[str, Any] = {"question": q["action"], "option": option, "channel": "ticktick",
                                   "task_id": q["id"]}
        if approve and q["action"].casefold().startswith(APPROVE_PREFIX):
            current = self._plan_sha(q["project"], q["action"])
            if current is None or current != q.get("plan_sha256"):
                return False
            payload["plan_sha256"] = current
            payload["approved_by"] = "owner"
            with self.store.tx() as tx:
                tx.record_event(kind="intent.ops_approved", project=q["project"], payload=payload)
        with self.store.tx() as tx:
            tx.record_event(kind="owner.ticktick_decision", project=q["project"], payload=payload)
        return True

    def _announce_result(self, card_id: str, intent: dict, rec: dict) -> None:
        """Once per result: the owner's own card keeps its text, so the result goes as a comment."""
        text = intent.get("done_text") or ""
        if rec is self.state.get("owner_cards", {}).get(card_id):
            rec.update(handed_over=True, done_text=text)
        told = self.state.setdefault("announced", {})
        if told.get(card_id) == _digest(text):
            return
        self._comment(card_id, f"Флот · {self._stamp()} · готово: {_short(text, 600)}\n{ACCEPT_HINT}")
        told[card_id] = _digest(text)
        self._save()

    def _comment(self, task_id: str, text: str) -> None:
        answer = self.api.call("add_comment", {"project_id": self.state["list_id"],
                                               "task_id": task_id, "title": text[:1000]})
        if isinstance(answer, dict) and answer.get("id"):
            self._wrote("comment", answer["id"])

    def _stamp(self) -> str:
        return self.clock().strftime("%d.%m %H:%M")

    # -- one cycle -------------------------------------------------------------------------
    def sync(self, snapshot: dict, *, system_lines: list[str]) -> dict:
        self._ensure_list()
        self._ensure_tags()
        stamp = self.clock().strftime("%H:%M")
        system = self._ensure_column("system", SYSTEM_COLUMN, 1_000_000)
        self._ensure_column("inbox", INBOX_COLUMN, -1_000_000)
        self._board = self.api.call("filter_tasks", {"filter": {
            "projectIds": [self.state["list_id"]], "status": [0]}}) or []
        self.read_owner_cards(self._board)
        pins = {}
        keys_by_column = {cid: key for key, cid in self.state["columns"].items()}
        for rec in self.state.get("owner_cards", {}).values():
            key = keys_by_column.get(rec.get("column") or "")
            if key and key.startswith("s:") and rec.get("section"):
                pins[rec["file"]] = (key[2:], rec["section"])
        sections = fleet_sections(self.store, snapshot, pins, self.state.get("accepted"))
        current = {"s:" + s["key"] for s in sections}
        stale = [k for k in self.state["columns"] if k not in ("system", "inbox") and k not in current]
        spare = self.state.setdefault("spare_columns", [])
        spare.extend(self.state["columns"][k] for k in stale)
        for key in stale:
            note_id = self.state["notes"].pop("now:" + key[2:], None)
            if note_id:
                try:
                    self.api.call("delete_task", {"project_id": self.state["list_id"], "task_id": note_id})
                except BoardUnavailable:
                    pass
            del self.state["columns"][key]
        by_project: dict[str, dict] = {}
        for order, section in enumerate(sections):
            column = self._ensure_column("s:" + section["key"], section["name"], order * 10, spare)
            self._upsert_note("now:" + section["key"], column, now_title(section),
                              now_lines(section, stamp), -100)
            for intent in section["intents"]:
                card_id = self._ensure_card(intent, column)
                rec = (self.state.get("owner_cards", {}).get(card_id)
                       or self.state.get("card_states", {}).get(card_id) or {})
                if intent.get("handed_over"):
                    self._announce_result(card_id, intent, rec)
                self._merge_tags(card_id, state_tag(intent, section, paused=rec.get("state") == "paused"))
                by_project[intent["project"]] = intent
                for action in intent["actions"]:
                    self._ensure_question(card_id, action, intent.get("brief"))
                if not intent.get("handed_over"):  # a finished intent shows its result instead
                    self._ensure_brief(intent)
        names = self.state.setdefault("column_names", {})
        for column_id in spare:
            if names.get(column_id) != ["· свободно", 2_000_000]:
                self.api.call("update_column", {"project_id": self.state["list_id"], "column_id": column_id,
                                                "column": {"id": column_id, "name": "· свободно",
                                                           "sortOrder": 2_000_000}})
                names[column_id] = ["· свободно", 2_000_000]
        decided = self.read_decisions()
        self.read_card_ticks(by_project)
        self.read_owner_items(sections)
        self.read_comments()
        self._board = None
        self._upsert_note("system", system, "▶ Флот", "\n".join([*system_lines, f"— сверка {stamp}"]), -100)
        self._upsert_note("help", system, "Как работать с доской", HELP, -200)
        self._save()
        return {"sections": len(sections), "decided": decided}


def system_lines(snapshot: dict, oauth_status: dict) -> list[str]:
    fleet = snapshot.get("human", {}).get("fleet", {})
    left = oauth_status.get("days_left")
    key = "ключ TickTick: " + (f"ещё {left:g} дн." if isinstance(left, (int, float)) else "бессрочный")
    if isinstance(left, (int, float)) and left < 7:
        key += " — скоро переподключить (dev-mcp\\ticktick-connect.cmd)"
    return [f"агентов: {fleet.get('agents', 0)} · очередь: {fleet.get('queue', 0)}", key]


STATUS_PATH = STATE_PATH.with_name("ticktick-board-status.json")


def _status(data: dict) -> None:
    try:
        STATUS_PATH.write_text(json.dumps({"at": datetime.now(timezone.utc).isoformat(), **data},
                                          ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def run_worker(store: Any, stop_event: Any) -> None:
    """Server-side board sync; sleeps quietly until the owner connects TickTick."""
    from dev_mcp.owner import ticktick_oauth as oauth
    from dev_mcp.owner.overview import snapshot as overview_snapshot
    from urllib.request import ProxyHandler, build_opener
    from dev_mcp.owner.ticktick import StreamableHTTP, _NoRedirect
    board: Board | None = None
    failures = 0
    while not stop_event.is_set():
        delay = SYNC_INTERVAL_S
        started = time.monotonic()
        before = board.api.requests if board is not None else 0
        try:
            if oauth.status().get("connected"):
                if board is None:
                    # The machine-wide proxy silently hangs some domains: go direct.
                    transport = StreamableHTTP(timeout=30.0, opener=build_opener(
                        ProxyHandler({}), _NoRedirect()))
                    board = Board(store, Api(transport, oauth.credential))
                snap = overview_snapshot(store)
                if snap.get("status") in {"ready", "empty"}:
                    result = board.sync(snap, system_lines=system_lines(snap, oauth.status()))
                    _status({"ok": True, **result,
                             "requests": board.api.requests - before,
                             "cycle_seconds": round(time.monotonic() - started, 3),
                             "retry_after_seconds": SYNC_INTERVAL_S})
                    failures = 0
            else:
                board = None
                _status({"ok": False, "error": "not connected"})
        except Exception as exc:
            failures += 1
            delay = max(min(SYNC_INTERVAL_S * 2 ** min(failures - 1, 4), 900),
                        float(getattr(exc, "retry_after_s", 0)))
            LOG.exception("ticktick board sync deferred")
            # The server log is not where anyone looks: fleet.cmd and Claude read this file.
            _status({"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300],
                     "requests": board.api.requests - before if board is not None else 0,
                     "cycle_seconds": round(time.monotonic() - started, 3),
                     "retry_after_seconds": delay, "consecutive_failures": failures})
            board = None  # fresh MCP session next time
        stop_event.wait(delay)
