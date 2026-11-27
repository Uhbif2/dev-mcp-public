"""15.11: снимок preflight влезает в лимиты ступенями «рядом без хеша» (Beta App: 10 230 файлов)."""
from __future__ import annotations

import subprocess
from pathlib import Path

from dev_mcp.fleet import exec_preflight as e


def test_split_moves_docs_then_largest_beside(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "p"
    (root / "src").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "_attic").mkdir()
    for i in range(6):
        (root / "src" / f"m{i}.py").write_text("x" * (10 + i), encoding="utf-8")
    for i in range(4):
        (root / "docs" / f"d{i}.md").write_text("doc", encoding="utf-8")
    (root / "_attic" / "old.py").write_text("old", encoding="utf-8")
    (root / "a.png").write_bytes(b"png")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    monkeypatch.setattr(e, "_MAX_FILES", 5)
    kept, beside = e._snapshot_split(root)
    assert len(kept) <= 5 and all(k.startswith("src/") for k in kept)
    assert "a.png" in beside and "docs/d0.md" in beside and "_attic/old.py" in beside
    assert set(kept) | set(beside) == set(e._git_visible(root))
    assert "src/m5.py" in beside and "src/m0.py" in kept  # уходит крупнейший
