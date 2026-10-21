from types import SimpleNamespace

from dev_mcp.request_triage import decide


def _policy():
    return SimpleNamespace(roots=[r"C:\hub"], write_roots=[r"C:\hub\_todo"], exec_roots=[],
                           tree_roots=[r"C:\hub"], tree_deny=[r"C:\hub\dev-mcp"])


def test_tree_root_path_is_already_allowed() -> None:
    verdict, _ = decide({"kind": "access", "path": r"C:\hub\local-library",
                         "lists": ["write_roots", "exec_roots"]}, _policy())
    assert verdict == "approved"


def test_tree_deny_stays_denied() -> None:
    verdict, _ = decide({"kind": "access", "path": r"C:\hub\dev-mcp\src",
                         "lists": ["write_roots"]}, _policy())
    assert verdict == "denied"
