from __future__ import annotations

import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Task:
    project: str
    tz: str
    state: str
    priority: int
    kind: str
    spec_path: str
    spec_sha: str
    needs: tuple[str, ...]
    owns: tuple[str, ...]
    resources: tuple[str, ...]
    acceptance: tuple[str, ...]
    title: str
    body: str


@dataclass(frozen=True)
class Worker:
    id: str
    label: str
    kind: str
    revoked: bool
    revoke_reason: str


@dataclass(frozen=True)
class Message:
    id: str
    kind: str
    sender: str
    body: str
    age_min: int


@dataclass(frozen=True)
class Decision:
    allow: bool
    code: str
    reason: str
    worker_id: str | None
    degraded: bool


_DRIVE_ROOT_RE = re.compile(r"^[A-Za-z]:/$")


def _collapse_slashes(path: str) -> str:
    """Collapse duplicate separators without destroying a UNC prefix."""
    unc = path.startswith("//")
    collapsed = re.sub(r"/+", "/", path)
    if unc and not collapsed.startswith("//"):
        collapsed = "/" + collapsed
    return collapsed


def _strip_trailing_slash(path: str) -> str:
    if path in {"", "/"} or _DRIVE_ROOT_RE.fullmatch(path):
        return path
    return path.rstrip("/")


def norm_path(raw: str, *, project_root: str | None = None) -> str:
    """Return the canonical fleet representation of a filesystem path.

    A trailing /** is the only supported glob form and is preserved in
    the returned value. Relative paths require an explicit project root.
    """
    if not isinstance(raw, str) or not raw:
        raise ValueError("path must be a non-empty string")

    path = _collapse_slashes(raw.replace("\\", "/"))
    path = _strip_trailing_slash(path)

    subtree = path.endswith("/**")
    base = path[:-3] if subtree else path
    base = _strip_trailing_slash(base)

    if any(token in base for token in ("*", "?", "[")):
        raise ValueError("only a trailing '/**' glob is allowed")
    if not subtree and any(token in path for token in ("*", "?", "[")):
        raise ValueError("only a trailing '/**' glob is allowed")
    if not base:
        raise ValueError("path must not be empty")

    native_base = base.replace("/", os.sep)
    if not os.path.isabs(native_base):
        if project_root is None:
            raise ValueError("relative path requires project_root")
        native_base = os.path.join(os.fspath(project_root), native_base)

    resolved = os.path.realpath(native_base)
    canonical = _collapse_slashes(resolved.replace("\\", "/"))
    canonical = _strip_trailing_slash(canonical).casefold()
    if subtree:
        canonical += "/**"
    return canonical


def _overlap_segments(path: str) -> list[str]:
    path = _collapse_slashes(path.replace("\\", "/"))
    path = _strip_trailing_slash(path)
    if path.endswith("/**"):
        path = _strip_trailing_slash(path[:-3])
    return path.casefold().split("/")


def paths_overlap(a: str, b: str) -> bool:
    """Paths overlap when one is a segment-prefix of the other.

    This is the Nova algorithm with fleet's canonical
    case-insensitive/separator handling applied before the segment-prefix
    comparison.
    """
    sa, sb = _overlap_segments(a), _overlap_segments(b)
    n = min(len(sa), len(sb))
    return sa[:n] == sb[:n]


# Задачи, у которых номер — часть собственного имени (styk:4, request:1a2b3c4d), а не номер
# раунда. Без этого «fix:styk:4» читался как 4-й fix задачи «styk»: `fleet.cmd result` терял fix
# из цепочки, интегратор — пути стыка, независимость ревью склеивала все стыки в одну семью (07.10).
_NAMED_ROOT = re.compile(r"^((?:styk|triage):\d+|request:[0-9A-Za-z]+)")
_ROUND_SUFFIX = re.compile(r"(?::\d{1,3})+$")


def task_root(tz: str) -> str:
    """review:fix:TZ-04:2 → TZ-04; fix:styk:4 → styk:4; review:request:ab12cd34:2 → request:ab12cd34."""
    text = str(tz or "")
    while text.startswith(("review:", "fix:")):
        text = text.split(":", 1)[1]
    named = _NAMED_ROOT.match(text)
    return named.group(1) if named else _ROUND_SUFFIX.sub("", text)
