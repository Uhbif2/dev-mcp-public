"""apply_patch: разбор формата Codex и применение фрагментов к тексту."""
from __future__ import annotations

import pytest

from dev_mcp.patch import PatchError, apply, parse

SRC = "def a():\n    return 1\n\n\ndef b():\n    x = 1\n    return x\n"


def test_update_with_anchor_and_add() -> None:
    ops = parse(
        "*** Begin Patch\n"
        "*** Update File: m.py\n"
        "@@ def b():\n"
        "-    x = 1\n"
        "+    x = 2\n"
        "     return x\n"
        "*** Add File: n.py\n"
        "+print('hi')\n"
        "*** End Patch\n"
    )
    assert [(o.kind, o.path) for o in ops] == [("update", "m.py"), ("add", "n.py")]
    text, added, removed = apply(SRC, ops[0])
    assert "    x = 2\n    return x\n" in text
    assert text.startswith("def a():\n    return 1\n")
    assert (added, removed) == (1, 1)
    assert ops[1].content == "print('hi')\n"


def test_anchor_picks_right_occurrence() -> None:
    src = "def a():\n    return x\n\ndef b():\n    return x\n"
    op = parse("*** Begin Patch\n*** Update File: m.py\n@@ def b():\n-    return x\n+    return y\n*** End Patch")[0]
    text, _, _ = apply(src, op)
    assert text == "def a():\n    return x\n\ndef b():\n    return y\n"


def test_tolerates_trailing_whitespace_and_code_fences() -> None:
    op = parse("```\n*** Begin Patch\n*** Update File: m.py\n-    return 1   \n+    return 2\n*** End Patch\n```")[0]
    text, _, _ = apply(SRC, op)
    assert "    return 2\n" in text


def test_missing_context_names_fragment() -> None:
    op = parse("*** Begin Patch\n*** Update File: m.py\n-nope\n+yes\n*** End Patch")[0]
    with pytest.raises(PatchError, match="фрагмент 1"):
        apply(SRC, op)


@pytest.mark.parametrize("patch, msg", [
    ("*** Update File: m.py\n-a\n+b\n*** End Patch", "Begin Patch"),
    ("*** Begin Patch\n*** Delete File: m.py\n*** End Patch", "удаление"),
    ("*** Begin Patch\n*** Update File: m.py\n*** Move to: z.py\n-a\n+b\n*** End Patch", "перенос"),
    ("*** Begin Patch\n*** Add File: n.py\nno plus\n*** End Patch", "«\\+»"),
    ("*** Begin Patch\n*** Update File: m.py\n-a\n*** Update File: m.py\n-b\n*** End Patch", "дважды"),
])
def test_rejects_unsupported(patch: str, msg: str) -> None:
    with pytest.raises(PatchError, match=msg):
        parse(patch)


def test_end_of_file_chunk_appends() -> None:
    op = parse("*** Begin Patch\n*** Update File: m.py\n     return x\n+# end\n*** End of File\n*** End Patch")[0]
    text, added, _ = apply(SRC, op)
    assert text.endswith("    return x\n# end\n")
    assert added == 1
