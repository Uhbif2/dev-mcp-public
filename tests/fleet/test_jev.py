from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dev_mcp.fleet import jev  # noqa: E402


def test_no_key_means_unknown(monkeypatch) -> None:
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("DEV_MCP_JEV_KEY_FILE", raising=False)
    assert jev.owner_gate_probability("x" * 100) is None


def test_answer_parsed_and_failures_are_unknown(monkeypatch, tmp_path: Path) -> None:
    key = tmp_path / "k"
    key.write_text("secret\n", encoding="utf-8")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("DEV_MCP_JEV_KEY_FILE", str(key))
    seen = {}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(req, timeout):
        seen["auth"] = req.headers.get("Authorization")
        return Resp(json.dumps({"answers": {"o": {"noul": 0.91}}}).encode())

    monkeypatch.setattr(jev._opener, "open", fake_open)
    assert jev.owner_gate_probability("причина отказа: нужен реальный телефон владельца") == 0.91
    assert seen["auth"] == "Bearer secret"

    def broken(req, timeout):
        raise OSError("network down")

    monkeypatch.setattr(jev._opener, "open", broken)
    assert jev.owner_gate_probability("причина отказа: нужен реальный телефон владельца") is None
    assert jev.owner_gate_probability("коротко") is None  # слишком короткий текст — не спрашиваем
