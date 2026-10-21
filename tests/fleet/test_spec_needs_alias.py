"""needs находит ТЗ проекта, чьё имя во флоте не совпадает с именем папки (10.10.2026)."""
from pathlib import Path

from dev_mcp.fleet import spec


def _tz(root: Path, tz: str, needs: str) -> None:
    (root / "ToDo" / "tz").mkdir(parents=True, exist_ok=True)
    (root / "ToDo" / "tz" / f"{tz}-x.md").write_text(
        f"```yaml\ntz: {tz}\nowns: [src/{tz}.py]\nneeds: [{needs}]\nacceptance:\n  - echo ok\n```\n",
        encoding="utf-8")


def test_dependency_graph_ignores_folder_derived_name(tmp_path: Path) -> None:
    root = tmp_path / "dev-mcp-work"
    _tz(root, "TZ-1", "")
    _tz(root, "TZ-2", "TZ-1#contract")
    graph, known = spec._dependency_graph(root, "Fleet Platform")
    assert {"TZ-1", "TZ-2"} <= known
    assert graph["TZ-2"] == ("TZ-1",)
