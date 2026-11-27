"""Non-blocking lint for product-entry acceptance in code specifications."""

from __future__ import annotations

import re

from .model import Task


_SOURCE_SUFFIXES = (
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".rs", ".go", ".java", ".kt", ".cs", ".cpp", ".cc", ".c", ".h",
    ".hpp", ".swift", ".dart", ".vue", ".svelte",
)
_SOURCE_DIRS = ("src/", "lib/", "app/", "extension/", "packages/")
_SECTION_RE = re.compile(r"(?im)^#{1,6}\s*путь\s+продукта\s*(?:[:—-].*)?$")


def _owns_source(path: str) -> bool:
    value = path.replace("\\", "/").casefold().rstrip("/")
    parts = value.split("/")
    if any(part in ("test", "tests", "testdata", "fixtures", "__tests__")
           for part in parts):
        return False
    if value.endswith("/**"):
        return any(f"/{directory}" in f"/{value}" for directory in _SOURCE_DIRS)
    if not value.endswith(_SOURCE_SUFFIXES):
        return False
    name = parts[-1]
    return not (name.startswith(("test_", "conftest.")) or name.endswith((".test.py", ".spec.ts", ".test.ts", ".spec.js", ".test.js")))


def product_path_warning(task: Task) -> str | None:
    """Warn only for code owns, never reject legacy specifications."""
    if task.kind not in ("work", "fix"):
        return None
    if not any(_owns_source(path) for path in task.owns):
        return None
    if _SECTION_RE.search(task.body or ""):
        return None
    return ("warning: ТЗ с исходным кодом в owns: отсутствует раздел "
            "«Путь продукта» (продуктовая точка входа теста и контрпример, "
            "который тест обязан поймать)")
