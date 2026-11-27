"""04.11.2026: свидетели обещаний — система сама замеряет обещанное, ведущий видит красное первым."""
from __future__ import annotations

import sys
from pathlib import Path

from dev_mcp.fleet import witness
from dev_mcp.fleet.store import Store


def _setup(tmp_path: Path, monkeypatch, items):
    store = Store.open(str(tmp_path / "fleet.sqlite"))
    root = tmp_path / "proj"
    root.mkdir()
    with store.tx() as tx:
        tx.change("INSERT INTO projects(name,root,priority,active) VALUES('P',?,0,1)", (str(root),),
                  event="test_setup")
    monkeypatch.setattr(witness, "_config", lambda: {"P": items})
    return store, root


def test_verdict_from_last_json_line_and_missing_script(tmp_path, monkeypatch):
    store, root = _setup(tmp_path, monkeypatch, [])
    (root / "w.py").write_text('print("noise")\nprint(\'{"verdict": "broken", "summary": "старая версия"}\')\n',
                               encoding="utf-8")
    ok = witness.run_one(str(root), {"id": "a", "cmd": [sys.executable, "w.py"]})
    assert ok["verdict"] == "broken" and ok["summary"] == "старая версия"
    gap = witness.run_one(str(root), {"id": "b", "cmd": [sys.executable, "none.py"]})
    assert gap["verdict"] == "missing" and "никто не замеряет" in gap["summary"]
    (root / "bad.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
    assert witness.run_one(str(root), {"id": "c", "cmd": [sys.executable, "bad.py"]})["verdict"] == "error"


def test_lead_sees_regression_and_gaps_first(tmp_path, monkeypatch):
    items = [{"id": "upd", "obligation": "O-update", "cmd": ["py", "x.py"]},
             {"id": "chain", "obligation": "O-chain", "cmd": ["py", "y.py"]},
             {"id": "ok", "obligation": "O-ok", "cmd": ["py", "z.py"]}]
    store, _ = _setup(tmp_path, monkeypatch, items)
    with store.tx() as tx:
        for payload in ({"id": "upd", "verdict": "works_live"},
                        {"id": "upd", "verdict": "broken", "summary": "манифест истёк"},
                        {"id": "ok", "verdict": "works_live"}):
            tx.record_event(kind=witness.KIND, project="P", tz="witness:x", payload=payload)
    text = "\n".join(witness.lead_lines(store, "P"))
    assert "СЛОМАЛОСЬ БЕЗ ТЕБЯ" in text and "манифест истёк" in text
    assert "chain (O-chain): ещё не запускался" in text
    assert text.index("✗ upd") < text.index("? chain") < text.index("✓ works_live: ok")


def test_due_respects_interval_and_world_resources(tmp_path, monkeypatch):
    items = [{"id": "phone", "cmd": ["py", "a.py"], "every_h": 3, "resources": ["телефон"]},
             {"id": "local", "cmd": ["py", "b.py"], "every_h": 3}]
    store, _ = _setup(tmp_path, monkeypatch, items)
    from dev_mcp.fleet import world
    monkeypatch.setattr(world, "down_resources", lambda tx, res, cfg=None: list(res))
    assert [i["id"] for _, _, i in witness.due(store)] == ["local"]
    with store.tx() as tx:
        tx.record_event(kind=witness.KIND, project="P", tz="witness:local",
                        payload={"id": "local", "verdict": "works_live"})
    assert witness.due(store) == []


def test_explicit_unknown_preserves_reason_and_next_step(tmp_path, monkeypatch):
    store, root = _setup(tmp_path, monkeypatch, [])
    (root / "hold.py").write_text(
        'print(\'{"verdict":"unknown","reason":"live acquisition not built",'
        '"next_step":"implement authorized acquisition"}\')\nraise SystemExit(2)\n',
        encoding="utf-8")
    result = witness.run_one(str(root), {"id": "dossier", "cmd": [sys.executable, "hold.py"]})
    assert result["verdict"] == "unknown" and result["rc"] == 2
    assert result["summary"] == "live acquisition not built"
    assert result["next_step"] == "implement authorized acquisition"
    with store.tx() as tx:
        tx.record_event(kind=witness.KIND, project="P", payload=result)
    monkeypatch.setattr(witness, "_config", lambda: {"P": [{"id": "dossier", "cmd": ["py"]}]})
    text = "\n".join(witness.lead_lines(store, "P"))
    assert "live acquisition not built" in text and "implement authorized acquisition" in text


def test_nonzero_exit_cannot_claim_live_success(tmp_path, monkeypatch):
    _, root = _setup(tmp_path, monkeypatch, [])
    (root / "bad.py").write_text(
        'print(\'{"verdict":"works_live","summary":"all good"}\')\nraise SystemExit(1)\n',
        encoding="utf-8")
    result = witness.run_one(str(root), {"id": "bad", "cmd": [sys.executable, "bad.py"]})
    assert result["verdict"] == "error" and result["rc"] == 1
