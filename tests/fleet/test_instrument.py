"""LEAD-SYSTEM-DESIGN 2.3: прибор принимается двумя квитанциями — красный на поломке, зелёный на эталоне."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from dev_mcp.fleet import goals, instrument, project_config
from dev_mcp.fleet.store import Store

WITNESS = (
    "import json, pathlib\n"
    "ok = pathlib.Path('product.txt').read_text(encoding='utf-8').strip() == 'works'\n"
    "print(json.dumps({'verdict': 'works_live' if ok else 'broken', 'summary': 'x'}))\n"
)


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    root, broken = tmp_path / "Proj", tmp_path / "Broken"
    for tree, product in ((root, "works"), (broken, "broken")):
        (tree / "ops" / "witness").mkdir(parents=True)
        (tree / "ops" / "witness" / "w.py").write_text(WITNESS, encoding="utf-8")
        (tree / "product.txt").write_text(product, encoding="utf-8")
    cell = {"id": "w", "obligation": "O-a", "cmd": [sys.executable, "ops/witness/w.py"]}
    monkeypatch.setattr(project_config, "project_root", lambda _p: str(root))
    monkeypatch.setattr(goals, "project_info", lambda _p: {"witness": [cell]})
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    return store, root, broken, cell


def test_accept_needs_red_on_breakage_and_green_on_reference(env):
    store, root, broken, cell = env
    receipt = instrument.accept(store, "P", "w", str(broken))
    assert receipt["accepted"] and receipt["red"]["verdict"] == "broken"
    assert receipt["green"]["verdict"] == "works_live"
    assert instrument.audit(store, "P") == {"unpinned": ["w"], "unproven": []}
    cell["sha"] = receipt["sha"]
    assert instrument.audit(store, "P") == {"unpinned": [], "unproven": []}


def test_instrument_that_never_reddens_is_not_accepted(env):
    store, root, broken, cell = env
    (broken / "product.txt").write_text("works", encoding="utf-8")  # поломка не ловится
    assert not instrument.accept(store, "P", "w", str(broken))["accepted"]
    cell["sha"] = "f" * 64
    assert instrument.audit(store, "P")["unproven"] == ["w"]
    assert any("красный на поломке" in line for line in instrument.brief_lines(store, "P"))


def test_breakage_tree_must_carry_the_same_instrument(env):
    store, root, broken, cell = env
    (broken / "ops" / "witness" / "w.py").write_text(WITNESS + "# правка\n", encoding="utf-8")
    with pytest.raises(ValueError, match="другой байт прибора"):
        instrument.accept(store, "P", "w", str(broken))
