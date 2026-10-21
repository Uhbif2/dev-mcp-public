from dev_mcp.intents.stages import _clip


def test_clip_keeps_short_text_and_marks_cut_at_line():
    assert _clip("abc", 10) == "abc"
    text = "line one\n" * 50
    out = _clip(text, 100)
    assert out.endswith("[обрезано сервером]\n")
    assert "line on\n" not in out.split("\n[")[0][-10:]
