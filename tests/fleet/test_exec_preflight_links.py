"""Dependency links must not silently turn preflight into a source-tree writer."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from dev_mcp.fleet import exec_preflight as pre
from dev_mcp.fleet.gate_snapshot import SnapshotStore


def test_mutated_linked_dependency_rejects_otherwise_green_gate(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("pass\n", encoding="utf-8")
    vendor = tmp_path / "original-dependency"
    vendor.mkdir()
    original = vendor / "module.py"
    original.write_text("value = 1\n", encoding="utf-8")

    def runner(argv, **kwargs):
        linked = Path(kwargs["cwd"]) / "vendor" / "module.py"
        assert linked.read_text(encoding="utf-8") == "value = 1\n"
        linked.write_text("value = 2\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    result = pre.preflight_gate(
        "python main.py", source_root=source,
        snapshots=SnapshotStore(tmp_path / "snapshots"),
        include=["main.py"], deps={"vendor": vendor}, runner=runner,
    )
    assert not result.ok
    assert "dependency" in result.reason
    assert original.read_text(encoding="utf-8") == "value = 2\n"


def test_unmodified_linked_dependency_remains_accepted(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("pass\n", encoding="utf-8")
    vendor = tmp_path / "original-dependency"
    vendor.mkdir()
    (vendor / "module.py").write_text("value = 1\n", encoding="utf-8")

    def runner(argv, **kwargs):
        assert (Path(kwargs["cwd"]) / "vendor" / "module.py").read_text() == "value = 1\n"
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    result = pre.preflight_gate(
        "python main.py", source_root=source,
        snapshots=SnapshotStore(tmp_path / "snapshots"),
        include=["main.py"], deps={"vendor": vendor}, runner=runner,
    )
    assert result.ok, result
    assert (vendor / "module.py").read_text(encoding="utf-8") == "value = 1\n"


def test_internal_dependency_link_is_allowed_but_external_is_not(tmp_path: Path) -> None:
    vendor = tmp_path / "vendor"
    (vendor / "pkg").mkdir(parents=True)
    (vendor / "pkg" / "index.js").write_text("ok", encoding="utf-8")
    internal = vendor / "internal"
    external = tmp_path / "external"
    external.mkdir()
    (external / "private.txt").write_text("secret", encoding="utf-8")
    outside_link = vendor / "outside"
    try:
        pre.link_dir(vendor / "pkg", internal)
        pre.link_dir(external, outside_link)
    except (OSError, NotImplementedError) as exc:
        pre.unlink_dir(internal)
        pre.unlink_dir(outside_link)
        pytest.skip(f"directory links unavailable: {exc}")
    try:
        with pytest.raises(pre.SnapshotError, match="escapes"):
            pre._dependency_fingerprints({"vendor": vendor})
        pre.unlink_dir(outside_link)
        assert "vendor" in pre._dependency_fingerprints({"vendor": vendor})
    finally:
        pre.unlink_dir(internal)
        pre.unlink_dir(outside_link)
    assert (external / "private.txt").read_text(encoding="utf-8") == "secret"


def test_dependency_verification_budget_marks_unverified(tmp_path: Path, monkeypatch) -> None:
    # Огромный node_modules не должен ронять preflight: дерево сверх бюджета — «не проверено».
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    (vendor / "one.txt").write_text("123", encoding="utf-8")
    (vendor / "two.txt").write_text("456", encoding="utf-8")
    monkeypatch.setattr(pre, "_DEP_CHECK_MAX_FILES", 1)
    first = pre._dependency_fingerprints({"vendor": vendor})
    (vendor / "two.txt").write_text("changed", encoding="utf-8")
    assert pre._dependency_fingerprints({"vendor": vendor}) == first


def test_dependency_write_through_link_changes_fingerprint(tmp_path: Path) -> None:
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    (vendor / "one.txt").write_text("123", encoding="utf-8")
    before = pre._dependency_fingerprints({"vendor": vendor})
    (vendor / "one.txt").write_text("1234", encoding="utf-8")
    assert pre._dependency_fingerprints({"vendor": vendor}) != before


def test_remove_dependency_link_preserves_original_directory(tmp_path: Path) -> None:
    vendor = tmp_path / "original-dependency"
    vendor.mkdir()
    (vendor / "keep.txt").write_text("untouched", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    links = pre.link_deps(work, {"vendor": vendor})
    assert len(links) == 1
    try:
        assert (work / "vendor" / "keep.txt").read_text() == "untouched"
    finally:
        for link in links:
            pre.unlink_dir(link)
    assert not (work / "vendor").exists()
    assert (vendor / "keep.txt").read_text(encoding="utf-8") == "untouched"


def test_unlink_failure_is_not_silently_ignored(tmp_path: Path, monkeypatch) -> None:
    vendor = tmp_path / "original-dependency"
    vendor.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    link = pre.link_deps(work, {"vendor": vendor})[0]
    try:
        if pre.os.name == "nt":
            monkeypatch.setattr(pre.os, "rmdir", lambda path: (_ for _ in ()).throw(PermissionError("locked")))
        else:
            monkeypatch.setattr(Path, "unlink", lambda self: (_ for _ in ()).throw(PermissionError("locked")))
        with pytest.raises(PermissionError, match="locked"):
            pre.unlink_dir(link)
    finally:
        monkeypatch.undo()
        pre.unlink_dir(link)

