import os
import time

from dev_mcp.fleet import temp_gc


def _mk(root, name, age_s, now, readonly=False):
    path = root / name
    (path / "sub").mkdir(parents=True)
    f = path / "sub" / "x.txt"
    f.write_text("x")
    if readonly:
        os.chmod(f, 0o444)
    os.utime(path, (now - age_s, now - age_s))
    return path


def test_keeps_two_newest_per_group_and_young_dirs(tmp_path):
    now = time.time()
    old = [_mk(tmp_path, f"fleet-integrate-aaaaaa{i:02d}", 86400 + i * 60, now, readonly=True)
           for i in range(5)]
    young = _mk(tmp_path, "fleet-preflight-yyyyyy01", 60, now)
    young2 = _mk(tmp_path, "fleet-preflight-yyyyyy02", 120, now)
    young3 = _mk(tmp_path, "fleet-preflight-yyyyyy03", 180, now)
    other = _mk(tmp_path, "notfleet-zzzzzz01", 999999, now)
    result = temp_gc.sweep(tmp_path, now=now)
    assert result == {"candidates": 3, "removed": 3, "failed": 0}
    assert [p.exists() for p in old] == [True, True, False, False, False]
    assert young.exists() and young2.exists() and young3.exists()  # в работе
    assert other.exists()


def test_files_and_missing_root_are_ignored(tmp_path):
    (tmp_path / "fleet-file-abcdefgh").write_text("x")
    assert temp_gc.sweep(tmp_path / "absent") == {"candidates": 0, "removed": 0, "failed": 0}
    assert temp_gc.sweep(tmp_path)["candidates"] == 0


def test_group_keys_python_and_node_suffixes():
    assert temp_gc.group_key("fleet-integrate-ef81xyqw") == "fleet-integrate-"
    assert temp_gc.group_key("fleet-tabs-dom-8f5VKc") == "fleet-tabs-dom-"
    assert temp_gc.group_key("fleet-snapshot-index") == "fleet-snapshot-index"


def test_remove_tree_deletes_read_only_git_objects_which_rmtree_ignore_errors_left(tmp_path):
    # 08.11: источник утечек %TEMP% — read-only объекты (preflight-objects) в fleet-integrate-*.
    import os, shutil, stat
    from dev_mcp.fleet.temp_gc import remove_tree

    def tree(name):
        root = tmp_path / name / "preflight-objects" / "ab"
        root.mkdir(parents=True)
        obj = root / "cdef"
        obj.write_bytes(b"blob")
        os.chmod(obj, stat.S_IREAD)
        return tmp_path / name

    old = tree("old")
    shutil.rmtree(old, ignore_errors=True)
    if os.name == "nt":
        assert old.exists()  # отрицательный контроль: прежняя уборка дерево оставляла
    new = tree("new")
    assert remove_tree(new) is True and not new.exists()
    assert remove_tree(tmp_path / "absent") is True
    remove_tree(old)


def test_remove_tree_does_not_follow_junction_into_target(tmp_path):
    import os
    from dev_mcp.fleet.temp_gc import remove_tree
    target = tmp_path / "venv"
    target.mkdir()
    (target / "keep.txt").write_text("keep", encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    link = scratch / ".venv"
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)
    assert remove_tree(scratch) is True
    assert (target / "keep.txt").read_text(encoding="utf-8") == "keep"
