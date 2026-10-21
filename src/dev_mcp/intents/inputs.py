"""Referenced intent inputs: metadata report and claim-scoped read grants.

The Markdown report is informational and is NEVER an authority source. Grants
live in the server-side fleet registry, written only by trusted intent spawn.
"""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

from dev_mcp.fleet.store import Store
from dev_mcp.policy import Policy, PolicyError


_URL = re.compile(r"https?://[^\s<>\"'\x60]+", re.IGNORECASE)
_QUOTED = re.compile(r"""(["'\x60])((?:[A-Za-z]:[\\/]|/)[^\r\n"'\x60]+)\1""")
_BARE = re.compile(
    r"""(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|(?<![:/])/(?!/))[^\s<>\[\]{}(),;\"'\x60]+"""
)
_WINDOWS_ABS = re.compile(r"^[A-Za-z]:[\\/]")


def _paths(text: str) -> tuple[str, ...]:
    """Collect explicit absolute paths, including ones containing spaces in quotes."""
    quoted = list(_QUOTED.finditer(text))
    candidates: list[tuple[int, str]] = [(m.start(), m.group(2)) for m in quoted]
    for match in _BARE.finditer(text):
        if any(a.start() <= match.start() < a.end() for a in quoted):
            continue
        raw = match.group().rstrip(".,:;!?")
        # Unquoted paths often contain spaces (e.g. D:\\Моя база знаний).
        # Prefer the longest *existing* prefix of the surrounding clause; for
        # absent files a terminal extension/comma still identifies the phrase.
        clause = re.split(r"[,;\r\n\]})]", text[match.start():], maxsplit=1)[0].strip()
        if " " in clause:
            choices = [clause, *(
                clause[:space.start()].strip()
                for space in reversed(list(re.finditer(r"\s+", clause)))
            )]
            found = next(
                (value for value in choices
                 if (candidate := _real(value)) is not None and candidate.exists()),
                None,
            )
            if found is not None:
                raw = found
            elif re.search(r"\.[a-zA-Z0-9]{1,10}\Z", clause):
                raw = clause
            elif text[match.start() + len(clause):].startswith((",", ";")):
                raw = clause
        candidates.append((match.start(), raw))
    seen: set[str] = set()
    result: list[str] = []
    for _, raw in sorted(candidates):
        if not raw or raw in seen:
            continue
        seen.add(raw)
        result.append(raw)
    return tuple(result)


def _real(raw: str) -> Path | None:
    """Canonicalize paths without falling back from a foreign OS drive."""
    if raw.startswith(("\\\\", "//", "\\\\?\\", "\\\\.\\")) or "\x00" in raw:
        return None
    if _WINDOWS_ABS.match(raw):
        if os.name != "nt":
            return None
        if ":" in raw[2:]:
            return None  # ADS
    elif not raw.startswith("/") or os.name == "nt":
        return None
    path = Path(raw)
    if ".." in path.parts or not path.is_absolute():
        return None
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None


def _key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _link_kind(url: str) -> str:
    lower = url.casefold()
    if "docs.google.com/spreadsheets/" in lower:
        return "Google Sheets — коннектор core"
    if "t.me/" in lower or "telegram.me/" in lower:
        return "Telegram — браузер или коннектор"
    return "Сайт — браузер или свой код"


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def record_inputs(store: Store, project: str, directory: Path, intent: str) -> Path:
    """Write a no-copy inventory and replace this project's server-side grants."""
    entries: list[tuple[str, str, bool, str, str]] = []
    grants: dict[str, bool] = {}
    for raw in _paths(intent):
        real = _real(raw)
        if real is None:
            entries.append((raw, "не поддерживается", False, "—", "—"))
            continue
        try:
            exists = real.exists()
            is_dir = real.is_dir() if exists else False
            size = "—" if not exists or is_dir else str(real.stat().st_size)
        except OSError:
            exists, is_dir, size = False, False, "—"
        entries.append((raw, "есть" if exists else "не найден", is_dir,
                        size, "папка" if is_dir else ("файл" if exists else "неизвестно")))
        # Missing paths are recorded, but acquire no access until a new trusted
        # spawn revalidates them. A missing directory must not grant a subtree.
        if exists:
            grants[_key(real)] = is_dir

    lines = [
        "# Входные материалы интенции", "",
        "Файлы не копируются. Доступ только на чтение задачам этого проекта; "
        "маски секретов политики действуют всегда.", "",
        "## Локальные пути", "",
        "| Путь | Наличие | Размер (байт) | Тип |",
        "| --- | --- | ---: | --- |",
    ]
    for raw, status, _, size, kind in entries:
        lines.append(f"| `{_cell(raw)}` | {status} | {size} | {kind} |")
    if not entries:
        lines.append("| — | нет ссылок | — | — |")
    lines.extend(["", "## Внешние ссылки", "",
                  "| Ссылка | Как получить |", "| --- | --- |"])
    urls = list(dict.fromkeys(m.group().rstrip(".,;!?)") for m in _URL.finditer(intent)))
    for url in urls:
        lines.append(f"| {_cell(url)} | {_link_kind(url)} |")
    if not urls:
        lines.append("| — | нет ссылок |")
    report_dir = directory / ".intent"
    report_dir.mkdir(exist_ok=True)
    target = report_dir / "inputs.md"
    temp = report_dir / f".inputs-{uuid.uuid4().hex}.tmp"
    try:
        temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        # First commit to the trusted registry, not a mutable Markdown file.
        with store.tx() as tx:
            tx.change(
                "CREATE TABLE IF NOT EXISTS intent_input_grants "
                "(project TEXT NOT NULL, path TEXT NOT NULL, is_dir INTEGER NOT NULL, "
                "PRIMARY KEY(project,path))",
                event="intent.inputs_schema",
            )
            tx.change("DELETE FROM intent_input_grants WHERE project=?",
                      (project,), event="intent.inputs_replace")
            for path, is_dir in grants.items():
                tx.change(
                    "INSERT INTO intent_input_grants(project,path,is_dir) VALUES(?,?,?)",
                    (project, path, int(is_dir)), event="intent.inputs_grant",
                )
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)
    return target


def resolve_granted_input(store: Store, policy: Policy, agent: str, raw: str) -> Path:
    """Resolve one read to an explicitly referenced input of the active claim."""
    real = _real(raw)
    if real is None:
        raise PolicyError("внешний вход: нужен безопасный абсолютный локальный путь")
    # Never bypass the ordinary policy for a path inside its regular roots:
    # a deny, write, or claim-checkout refusal there must remain a refusal.
    if policy._owning_root(real) is not None:
        raise PolicyError("внешний вход: обычные корни политики имеют приоритет")
    for part in real.parts:
        denied = policy.denied_by(part)
        if denied:
            raise PolicyError(f"внешний вход: путь закрыт маской «{denied}»")
    if not agent:
        raise PolicyError("внешний вход: требуется активная задача проекта")
    with store.read() as tx:
        claim = tx.one(
            "SELECT c.project FROM claims c JOIN workers w ON w.id=c.worker_id "
            "WHERE c.worker_id=? AND c.state='active' AND w.revoked_at IS NULL "
            "ORDER BY c.id DESC LIMIT 1", (agent,),
        )
        if claim is None:
            raise PolicyError("внешний вход: нет активной задачи проекта")
        # Old databases are legitimate, and deliberately have no grants.
        if not tx.scalar("SELECT 1 FROM sqlite_master WHERE type='table' "
                         "AND name='intent_input_grants'"):
            raise PolicyError("внешний вход: нет разрешённых ссылок")
        grants = tx.query("SELECT path,is_dir FROM intent_input_grants WHERE project=?",
                          (claim["project"],))
    target = _key(real)
    for row in grants:
        prefix = str(row["path"])
        if target == prefix or (
            bool(row["is_dir"]) and target.startswith(prefix.rstrip(os.sep) + os.sep)
        ):
            return real
    raise PolicyError("внешний вход: путь не указан в интенции этого проекта")
