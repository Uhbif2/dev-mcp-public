"""Кусающий тест fix: проверка в отдельной копии рабочего дерева, а не HEAD."""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from dev_mcp.fleet import bite, evidence
from dev_mcp.fleet.model import norm_path

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="нужен git")


def _sha(path: str) -> str | None:
    p = Path(path)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Проект под git: в HEAD только README; база задачи (helper.py) не закоммичена — её приняли не всё."""
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("x\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "init")
    (root / "helper.py").write_text("BASE = 1\n", encoding="utf-8")  # вне зоны fix, не в HEAD
    lib = root / "lib.py"
    lib.write_text("from helper import BASE\ndef f():\n    return BASE\n", encoding="utf-8")  # дефект

    snap = tmp_path / "snap"
    (snap / "blobs").mkdir(parents=True)
    rejected = _sha(str(lib))
    (snap / "blobs" / rejected).write_bytes(lib.read_bytes())
    zone = [norm_path(str(lib)), norm_path(str(root / "test_lib.py"))]
    manifest = {"files": {zone[0]: rejected, zone[1]: None}}

    monkeypatch.setattr(evidence, "snapshot_root", lambda store: snap)
    monkeypatch.setattr(evidence, "_load_manifest", lambda root_, claim_id: manifest)
    monkeypatch.setattr(evidence, "claim_paths", lambda store, claim_id: zone)
    monkeypatch.setattr(evidence, "expand", lambda paths: (list(paths), []))
    monkeypatch.setattr(evidence, "_sha", lambda p: _sha(p))
    return root, tmp_path / "wt"


def _check(root: Path, wt: Path) -> dict:
    return bite.check(None, str(root), 7, [sys.executable, "test_lib.py"], wt_base=wt)


def test_bites_with_base_outside_head(project) -> None:
    root, wt = project
    (root / "lib.py").write_text("from helper import BASE\ndef f():\n    return BASE + 1\n", encoding="utf-8")
    (root / "test_lib.py").write_text("import lib\nassert lib.f() == 2\n", encoding="utf-8")
    out = _check(root, wt)
    assert out["verdict"] == "bites", out
    assert (out["rc_after"], out["rc_before"] != 0) == (0, True)
    # общее дерево не тронуто: ни stash, ни лишних worktree
    assert subprocess.run(["git", "-C", str(root), "stash", "list"], capture_output=True, text=True).stdout == ""
    assert not any(wt.iterdir())


def test_toothless_when_test_passes_on_rejected(project) -> None:
    root, wt = project
    (root / "lib.py").write_text("from helper import BASE\ndef f():\n    return BASE + 1\n", encoding="utf-8")
    (root / "test_lib.py").write_text("import lib\nassert callable(lib.f)\n", encoding="utf-8")
    assert _check(root, wt)["verdict"] == "toothless"


def test_only_tests_changed_is_unchecked(project) -> None:
    root, wt = project
    (root / "test_lib.py").write_text("import lib\nassert lib.f() == 1\n", encoding="utf-8")
    out = _check(root, wt)
    assert out["verdict"] == "unchecked" and "кусать нечего" in out["reason"]


def test_opt_out_and_note_command() -> None:
    assert bite.opted_out("bite: n/a — визуальная правка") == "визуальная правка"
    assert bite.from_note("сделано\nbite: py -3 -m pytest test/x.py -q") == "py -3 -m pytest test/x.py -q"
    assert bite.from_note("bite: n/a — визуал") == ""
    assert bite.from_note("bite: py -3 -m pytest t/test_a.py: падает до фикса") == "py -3 -m pytest t/test_a.py"
    assert bite.from_note("bite: `py -3 -m pytest t/test_a.py::test_x -q` — падает на старой") == \
        "py -3 -m pytest t/test_a.py::test_x -q"
