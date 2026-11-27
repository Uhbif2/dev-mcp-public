"""09.11.2026: rustup proxies (cargo.exe -> rustup.exe) must keep their own name as argv[0]."""
from __future__ import annotations

from pathlib import Path

from dev_mcp import policy

CARGO = r"C:\u\.cargo\bin\cargo.EXE"
RUSTUP = r"C:\u\.cargo\bin\rustup.exe"
LINK = r"C:\links\git.exe"
GIT = r"C:\Program Files\Git\cmd\git.exe"


def test_multicall_proxy_keeps_found_name(monkeypatch):
    monkeypatch.setattr(policy.shutil, "which", lambda name: CARGO)
    monkeypatch.setattr(policy.os.path, "realpath", lambda p: RUSTUP)
    assert Path(policy._which_real("cargo")) == Path(CARGO)


def test_ordinary_link_still_resolves_to_real_file(monkeypatch):
    monkeypatch.setattr(policy.shutil, "which", lambda name: LINK)
    monkeypatch.setattr(policy.os.path, "realpath", lambda p: GIT)
    assert Path(policy._which_real("git")) == Path(GIT)
