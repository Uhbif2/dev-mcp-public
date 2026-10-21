"""Claim-scoped visual inspection for fleet agents via the Gemini relay."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from . import gemini
from .claim_take_routing import ClaimTakeRouting, project_path_to_claim
from .model import norm_path

MAX_FRAMES = 12
MAX_FRAME_BYTES = 4 * 1024 * 1024
LOOKS_PER_HOUR = 60

DEFAULT_RUBRIC = (
    "Оцени видимый интерфейс по кадрам. Проверь: визуальная иерархия; "
    "отступы, выравнивание и сетка; контраст, светлая и тёмная темы; "
    "hover/press/focus, пустые состояния и ошибки; читаемость текста. "
    "Если дана последовательность кадров, оцени плавность анимаций, "
    "рывки и мигания. Отмечай конкретное место каждого дефекта, "
    "а не абстрактное пожелание."
)


def _reject(reason: str, *, last_error: dict | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "defects": [], "error": reason}
    if last_error is not None:
        result["last_error"] = last_error
    return result


def _inside(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def _mime(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def look_for_agent(store: Any, worker_id: str, paths: Sequence[str], rubric: str = "", *,
                   policy: Any | None = None) -> dict[str, Any]:
    """Inspect 1–12 owned frames; never read an unowned path or use a caller's claim id."""
    agent = str(worker_id or "").strip()
    if not agent:
        return _reject("нужен worker_id агента")
    if (not isinstance(paths, (list, tuple)) or not 1 <= len(paths) <= MAX_FRAMES
            or any(not isinstance(p, str) or not p.strip() for p in paths)):
        return _reject("передай от 1 до 12 путей к изображениям")
    if not isinstance(rubric, str):
        return _reject("rubric должен быть строкой")

    try:
        with store.read() as tx:
            claim = tx.one(
                "SELECT c.id,c.project,c.tz,p.root,t.kind,t.body FROM claims c "
                "JOIN projects p ON p.name=c.project "
                "JOIN tasks t ON t.project=c.project AND t.tz=c.tz "
                "JOIN workers w ON w.id=c.worker_id "
                "WHERE c.worker_id=? AND c.state='active' AND w.revoked_at IS NULL "
                "ORDER BY c.id DESC LIMIT 1", (agent,),
            )
            if claim is None:
                return _reject("у агента нет активного захвата")
            owned = tuple(str(r["path_norm"]) for r in tx.query(
                "SELECT path_norm FROM claim_paths WHERE claim_id=?", (claim["id"],)
            ))
            reviewing = claim["kind"] == "review"
            if reviewing:
                from .review import _review_target
                project, tz = _review_target(claim["tz"], claim["body"], claim["project"])
                source = tx.one("SELECT state,body FROM tasks WHERE project=? AND tz=?", (project, tz))
                if (project != claim["project"] or source is None or source["state"] != "submitted"
                        or str(source["body"] or "").startswith("[fleet-blind]")):
                    return _reject("исходная задача ревью не подтверждена или требует слепого протокола")
        if not owned and not reviewing:
            return _reject("у захвата нет разрешённых путей (owns)")
        context = ClaimTakeRouting(store).resolve_claim_context(agent, int(claim["id"]))
        root = context.project_root.resolve(strict=True)
        tree = context.tree_path.resolve(strict=True)
        images: list[tuple[str, bytes]] = []
        for name in paths:
            raw = Path(name)
            if ".." in raw.parts:
                return _reject(f"путь содержит переход к родителю: {name}")
            actual = project_path_to_claim(name, context, read_only=reviewing,
                                           owns=None if reviewing else owned)
            actual = actual.resolve(strict=True)
            if not _inside(actual, tree) or not actual.is_file():
                return _reject(f"файл вне рабочей зоны или не существует: {name}")
            source = root / actual.relative_to(tree)
            if policy is not None:
                policy.resolve(str(source), for_write=False)
            normalized = norm_path(str(source))
            if not reviewing and not any(normalized == own or normalized.startswith(own.rstrip("/") + "/")
                                        for own in owned):
                return _reject(f"файл вне owns: {name}")
            if actual.suffix.casefold() not in (".png", ".jpg", ".jpeg", ".webp"):
                return _reject(f"поддерживаются только PNG/JPEG/WebP: {name}")
            if actual.stat().st_size > MAX_FRAME_BYTES:
                return _reject(f"кадр больше 4 МБ: {name}")
            data = actual.read_bytes()
            if len(data) > MAX_FRAME_BYTES:
                return _reject(f"кадр больше 4 МБ: {name}")
            mime = _mime(data)
            if mime is None:
                return _reject(f"некорректный PNG/JPEG/WebP: {name}")
            images.append((mime, data))

        now = datetime.now(timezone.utc)
        threshold = (now - timedelta(hours=1)).isoformat()
        with store.tx() as tx:
            rows = tx.query(
                "SELECT ts FROM events WHERE kind='look' AND ts>=? ORDER BY ts ASC",
                (threshold,),
            )
            if len(rows) >= LOOKS_PER_HOUR:
                retry = datetime.fromisoformat(str(rows[0]["ts"])) + timedelta(hours=1)
                return _reject("бюджет глаз исчерпан, повтори после " +
                               retry.astimezone(timezone.utc).isoformat(timespec="seconds"))
            tx.record_event(kind="look", worker_id=agent, project=claim["project"],
                            tz=claim["tz"], tool="fleet_look",
                            payload={"frames": len(images)})
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        return _reject(f"не удалось проверить файлы и захват: {exc}")

    try:
        criteria = DEFAULT_RUBRIC + ("\nДополнительная рубрика агента:\n" + rubric[:8000]
                                      if rubric.strip() else "")
        result = gemini.look(images, criteria)
    except Exception as exc:
        return _reject(f"ошибка Gemini: {type(exc).__name__}: {exc}",
                       last_error=gemini.last_error())
    if result is None:
        return _reject("Gemini не смог оценить кадры", last_error=gemini.last_error())
    return result


__all__ = ["look_for_agent", "DEFAULT_RUBRIC"]
