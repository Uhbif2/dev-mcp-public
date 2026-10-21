from dev_mcp import stackdump


def test_dump_lists_current_thread(tmp_path):
    out = stackdump.dump(tmp_path / "s.txt")
    assert "test_dump_lists_current_thread" in out.read_text(encoding="utf-8")
