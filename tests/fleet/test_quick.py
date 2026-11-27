"""Быстрые агенты (fleet/quick.py): книга, права владельца в страже, инструменты, доставка ответа."""
from __future__ import annotations

import json
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from dev_mcp.fleet import guard, owner, quick
from dev_mcp.fleet.owner import secret_path

from test_tools import _make_server, _seed_task, _tool_fn


def _book(store) -> quick.Book:
    return quick.Book(Path(store.path).parent)


def test_lifecycle_orders_and_reports(tmp_path: Path) -> None:
    book = quick.Book(tmp_path)
    job = quick.create(book, owner="claude", prompt="почини тест", project="demo", root=str(tmp_path))
    assert job["id"] == "q1" and job["tag"] == "Q1"
    first = quick.orders(book)[0]
    assert first["label"] == "pult-q1" and first["url"] == "" and 'agent="Q1"' in first["send"]
    assert "почини тест" in first["send"]

    quick.report(book, {"id": "q1", "sent": True, "url": "https://chatgpt.com/c/abcdef12-0000"})
    assert quick.orders(book)[0]["send"] is None
    delivered = []
    assert quick.report(book, {"id": "q1", "answer": "готово"}, deliver=lambda j, a: delivered.append(a))["new_answer"]
    assert not quick.report(book, {"id": "q1", "answer": "готово"})["new_answer"]  # тот же ход
    assert delivered == ["готово"]
    got = quick.get(book, "Q1")
    assert got["state"] == "answered" and got["answers"] == 1 and got["url"].endswith("abcdef12-0000")

    quick.say(book, "1", "ещё проверь линтер", owner="claude")
    assert quick.orders(book)[0]["send"] == "ещё проверь линтер"
    quick.close(book, "q1", owner="claude")
    assert quick.orders(book)[0]["close"] is True
    assert "готово" in quick.render(quick.get(book, "q1"))


def test_owner_isolation(tmp_path: Path) -> None:
    book = quick.Book(tmp_path)
    quick.create(book, owner="W5", prompt="x")
    try:
        quick.say(book, "q1-w5", "чужое", owner="W6")
    except PermissionError:
        pass
    else:
        raise AssertionError("чужой владелец не должен писать в чат")
    assert [j["id"] for j in quick.listing(book, owner="W5")] == ["q1-w5"]
    assert quick.listing(book, owner="W6") == []


def test_claude_sessions_have_independent_numbers_and_short_commands(tmp_path: Path, monkeypatch) -> None:
    """CLI reads the session env; all stored IDs, worker tags and labels stay globally distinct."""
    server, store, root, _ = _make_server(tmp_path)
    del server
    spec = importlib.util.spec_from_file_location(
        "fleet_quick_cli_test", Path(__file__).resolve().parents[2] / "tools" / "fleet.py")
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    book = _book(store)
    legacy = quick.create(book, owner="claude", prompt="старое задание")
    assert legacy["id"] == "q1" and legacy["tag"] == "Q1"

    ns = SimpleNamespace(action="new", words=["новое"], project="", scope=[str(root)],
                         home="", after=None, timeout=0, all=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "7f2a91-0000-0000")
    assert cli._cmd_quick(Path(store.path), ns)[0] == 0
    first = quick.get(book, "q1-7f2a91", owner="claude:7f2a91")
    assert first["id"] == "q1-7f2a91" and first["tag"] == "Q1-7F2A91"

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "8e4b20-9999-0000")
    assert cli._cmd_quick(Path(store.path), ns)[0] == 0
    second = quick.get(book, "q1-8e4b20", owner="claude:8e4b20")
    assert second["id"] == "q1-8e4b20" and second["tag"] == "Q1-8E4B20"
    assert len({legacy["tag"], first["tag"], second["tag"]}) == 3
    labels = {j["label"] for j in quick.orders(book)}
    assert labels == {"pult-q1", "pult-q1-7f2a91", "pult-q1-8e4b20"}
    with store.read() as tx:
        assert tx.one("SELECT id FROM workers WHERE id=?", (first["tag"],)) is not None
        assert tx.one("SELECT id FROM workers WHERE id=?", (second["tag"],)) is not None

    assert quick.is_quick_agent(first["tag"])
    assert quick.norm_id("Q1-7F2A91") == first["id"]
    assert quick.tag_of(first["id"]) == first["tag"]
    assert quick.get(book, "q1-legacy", owner="claude:7f2a91")["id"] == "q1"
    assert quick.get(book, "q1", owner="claude")["id"] == "q1"
    assert [j["id"] for j in quick.listing(book, owner="claude:7f2a91")] == [first["id"]]
    try:
        quick.get(book, second["id"], owner="claude:7f2a91")
    except PermissionError:
        pass
    else:
        raise AssertionError("одна сессия не должна управлять другой")

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "7f2a91-0000-0000")
    ns.action, ns.words = "list", []
    assert "q1-7f2a91" in cli._cmd_quick(Path(store.path), ns)[1]
    assert "q1-8e4b20" not in cli._cmd_quick(Path(store.path), ns)[1]
    ns.all = True
    assert "q1-8e4b20" in cli._cmd_quick(Path(store.path), ns)[1]
    ns.all = False
    ns.action, ns.words = "say", [first["id"], "ещё", "задача"]
    assert cli._cmd_quick(Path(store.path), ns)[0] == 0
    assert quick.get(book, first["id"])["outbox"][-1] == "ещё задача"
    assert quick.get(book, second["id"])["outbox"] == [quick.first_message(second)]
    ns.action, ns.words = "show", [first["id"]]
    assert first["id"] in cli._cmd_quick(Path(store.path), ns)[1]
    ns.action, ns.words = "wait", [first["id"]]
    assert first["id"] in cli._cmd_quick(Path(store.path), ns)[1]
    # An old q1 remains addressable even when this session has its own q1.
    quick.report(book, {"id": "q1", "sent": True})
    quick.report(book, {"id": "q1", "answer": "старый ответ"})
    ns.words = ["q1-legacy"]
    code, message = cli._cmd_quick(Path(store.path), ns)
    assert code == 0 and "q1 [answered]" in message and "старый ответ" in message
    ns.action, ns.words = "close", [first["id"]]
    assert first["id"] in cli._cmd_quick(Path(store.path), ns)[1]
    assert quick.get(book, first["id"])["state"] == "closed"
    assert quick.get(book, second["id"])["state"] != "closed"

    assert quick.operator_roots(second) == (root,)
    assert guard.check_write(store, second["tag"], str(tmp_path / "not-owned.txt")).code == "FLEET_QUICK_SCOPE"
    assert guard.check_write(store, first["tag"], str(root / "after-close.txt")).code == "FLEET_QUICK_CLOSED"


def test_short_id_collision_refuses_close_without_touching_either_agent(tmp_path: Path, monkeypatch) -> None:
    """Regression: quick close q3 must not silently close q3-819b59."""
    spec = importlib.util.spec_from_file_location(
        "fleet_quick_ambiguous_close_test", Path(__file__).resolve().parents[2] / "tools" / "fleet.py")
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "819b59-xyz")
    book = quick.Book(tmp_path)
    for n in range(3):
        quick.create(book, owner="claude", prompt=f"legacy-{n}")
        quick.create(book, owner="claude:819b59", prompt=f"session-{n}")
    before = book.jobs()
    assert before["q3"]["state"] == before["q3-819b59"]["state"] == "new"

    ns = SimpleNamespace(action="close", words=["q3"], all=False)
    with pytest.raises(ValueError) as error:
        cli._cmd_quick(tmp_path / "fleet.sqlite", ns)
    assert all(part in str(error.value) for part in ("q3", "q3-legacy", "q3-819b59", "неоднознач"))
    assert book.jobs() == before  # no close, no cleared outbox, no changed timestamps

    # The ambiguity is not specific to close: say/show/wait must not guess either.
    for action, words in (("say", ["q3", "test"]), ("show", ["q3"]), ("wait", ["q3"])):
        ns.action, ns.words = action, words
        with pytest.raises(ValueError, match="q3-legacy"):
            cli._cmd_quick(tmp_path / "fleet.sqlite", ns)
    assert book.jobs() == before

    # A short number remains valid when there is no shared legacy collision.
    unique = quick.create(book, owner="claude:819b59", prompt="only session q4")
    assert unique["id"] == "q4-819b59"
    ns.action, ns.words = "close", ["q4"]
    assert cli._cmd_quick(tmp_path / "fleet.sqlite", ns)[0] == 0
    assert quick.get(book, unique["id"])["state"] == "closed"

    ns.action, ns.words = "close", ["q3-legacy"]
    assert cli._cmd_quick(tmp_path / "fleet.sqlite", ns)[0] == 0
    assert quick.get(book, "q3-legacy", owner="claude:819b59")["state"] == "closed"
    assert quick.get(book, "q3-819b59", owner="claude:819b59")["state"] == "new"
    ns.words = ["q3-819b59"]
    assert cli._cmd_quick(tmp_path / "fleet.sqlite", ns)[0] == 0
    assert quick.get(book, "q3-819b59")["state"] == "closed"


def test_worker_owners_number_independently_and_legacy_lookup(tmp_path: Path) -> None:
    book = quick.Book(tmp_path)
    old = quick.create(book, owner="W5", prompt="старый", project="demo")
    assert old["id"] == "q1-w5"
    a = quick.create(book, owner="W6", prompt="новый")
    b = quick.create(book, owner="W5", prompt="следующий")
    assert a["id"] == "q1-w6" and b["id"] == "q2-w5"
    assert quick.get(book, "q1", owner="W5")["id"] == old["id"]
    assert quick.get(book, "q1", owner="W6")["id"] == a["id"]


def test_quick_say_at_file_preserves_multiline_content_and_help(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "fleet_quick_say_file_test", Path(__file__).resolve().parents[2] / "tools" / "fleet.py")
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    parser = cli.build_parser()
    help_text = next(a for a in parser._subparsers._actions
                     if isinstance(a.choices, dict) and "quick" in a.choices).choices["quick"].format_help()
    assert "cmd.exe" in help_text and "quick say qN @файл" in help_text

    book = quick.Book(tmp_path)
    job = quick.create(book, owner="claude", prompt="начальная задача")
    content = "Первая строка\n" + ("Вторая строка с деталями.\n" * 150) + "Последняя строка"
    assert len(content) > 3000
    source = tmp_path / "многострочный текст.txt"
    source.write_text(content, encoding="utf-8")
    ns = SimpleNamespace(action="say", words=["q1", f"@{source}"], all=False)
    code, output = cli._cmd_quick(tmp_path / "fleet.sqlite", ns)
    assert code == 0 and job["id"] in output
    assert quick.get(book, "q1")["outbox"][-1] == content

    ns.words = ["q1", f"@{tmp_path / 'missing.txt'}"]
    assert cli._cmd_quick(tmp_path / "fleet.sqlite", ns)[0] == 2
    assert quick.get(book, "q1")["outbox"][-1] == content  # no accidental literal @file sent
    ns.words = ["q1", "обычный", "текст"]
    assert cli._cmd_quick(tmp_path / "fleet.sqlite", ns)[0] == 0
    assert quick.get(book, "q1")["outbox"][-1] == "обычный текст"


def test_multi_wait_returns_first_answer_without_touching_other_jobs(tmp_path: Path, monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location(
        "fleet_quick_wait_any_test", Path(__file__).resolve().parents[2] / "tools" / "fleet.py")
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    parsed = cli.build_parser().parse_args(["quick", "wait", "q1", "q2", "q3", "--all"])
    assert parsed.words == ["q1", "q2", "q3"] and parsed.all
    book = quick.Book(tmp_path)
    jobs = [quick.create(book, owner="claude", prompt=f"work {n}") for n in range(3)]
    for job in jobs:
        quick.report(book, {"id": job["id"], "sent": True})

    ns = SimpleNamespace(action="wait", words=["q1", "q2", "q3"],
                         after=None, timeout=2, all=False)
    calls = []

    def receive_second(_delay: float) -> None:
        calls.append("poll")
        quick.report(book, {"id": "q2", "answer": "результат второго"})

    monkeypatch.setattr(cli.time, "sleep", receive_second)
    code, output = cli._cmd_quick(tmp_path / "fleet.sqlite", ns)
    assert code == 0 and calls == ["poll"]
    assert "q2 [answered]" in output and "результат второго" in output
    assert "q1 [answered]" not in output and "q3 [answered]" not in output
    assert [quick.get(book, jid)["delivered"] for jid in ("q1", "q2", "q3")] == [0, 1, 0]
    assert [quick.get(book, jid)["state"] for jid in ("q1", "q3")] == ["working", "working"]

    # Repeating the same set does not return the already-consumed q2 answer.
    ns.timeout = 0
    assert cli._cmd_quick(tmp_path / "fleet.sqlite", ns)[0] == 1
    quick.report(book, {"id": "q3", "answer": "ответ третьего"})
    code, output = cli._cmd_quick(tmp_path / "fleet.sqlite", ns)
    assert code == 0 and "q3 [answered]" in output and "ответ третьего" in output
    assert quick.get(book, "q1")["delivered"] == 0


def test_multi_wait_all_requires_every_answer_and_keeps_cursor_on_timeout(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "fleet_quick_wait_all_test", Path(__file__).resolve().parents[2] / "tools" / "fleet.py")
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    book = quick.Book(tmp_path)
    for n in range(2):
        job = quick.create(book, owner="claude", prompt=f"work {n}")
        quick.report(book, {"id": job["id"], "sent": True})
    quick.report(book, {"id": "q2", "answer": "ранний ответ"})
    ns = SimpleNamespace(action="wait", words=["q1", "q2"], after=None, timeout=0, all=True)
    code, message = cli._cmd_quick(tmp_path / "fleet.sqlite", ns)
    assert code == 1 and "q1, q2" in message
    assert quick.get(book, "q2")["delivered"] == 0

    quick.report(book, {"id": "q1", "answer": "поздний ответ"})
    code, message = cli._cmd_quick(tmp_path / "fleet.sqlite", ns)
    assert code == 0 and "ранний ответ" in message and "поздний ответ" in message
    assert message.index("q1 [answered]") < message.index("q2 [answered]")
    assert quick.get(book, "q1")["delivered"] == quick.get(book, "q2")["delivered"] == 1
    assert cli._cmd_quick(tmp_path / "fleet.sqlite", ns)[0] == 1

    # Explicit --after still permits a deliberate replay/threshold override.
    ns.after = 0
    assert cli._cmd_quick(tmp_path / "fleet.sqlite", ns)[0] == 0


def test_guard_inherits_owner_claims_and_scope(tmp_path: Path) -> None:
    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root, tz="TZ-1")
    join, take = _tool_fn(server, "fleet_join"), _tool_fn(server, "fleet_take")
    join("lead-tab"); join("other-tab")
    assert "TZ-1" in take("W1")
    owned = str(root / "owned.txt")
    assert not guard.check_write(store, "W2", owned).allow  # чужой захват

    book = _book(store)
    quick.create(book, owner="W1", prompt="x", project="demo", root=str(root))
    ok = guard.check_write(store, "Q1-W1", owned)
    assert ok.allow and ok.code == ""  # захват владельца — свой, без «вне заявки»
    free = guard.check_write(store, "Q1-W1", str(root / "free.txt"))
    assert free.allow and free.code == ""
    outside = guard.check_write(store, "Q1-W1", str(tmp_path / "elsewhere.txt"))
    assert not outside.allow and outside.code == "FLEET_QUICK_SCOPE"

    quick.create(book, owner="claude", prompt="y", project="demo", root=str(root))
    assert not guard.check_write(store, "Q1", owned).allow  # у Claude захвата нет: чужое — чужое
    quick.close(book, "q1", owner="claude")
    assert guard.check_write(store, "Q1", str(root / "free.txt")).code == "FLEET_QUICK_CLOSED"


def test_tools_role_take_and_delivery(tmp_path: Path) -> None:
    server, store, root, _ = _make_server(tmp_path)
    _seed_task(store, root, tz="TZ-1")
    join = _tool_fn(server, "fleet_join")
    fq = _tool_fn(server, "fleet_quick")
    orders, report = _tool_fn(server, "fleet_quick_orders"), _tool_fn(server, "fleet_quick_report")
    join("tab-a")
    assert "для ведущего и агента с ролью владельца" in fq("W1", "new", ["demo", "сделай"])

    owner.grant(owner.grants_path(secret_path(tmp_path)), "W1", ttl_s=600)
    started = fq("W1", "new", ["demo", "сделай", "быстро"])
    assert "q1-w1 запущен" in started
    job = json.loads(orders())[0]
    assert "сделай быстро" in job["send"] and job["label"] == "pult-q1-w1"
    assert job["recentMcp"] is False  # регистрация Q1 ещё не является вызовом инструмента

    assert "быстрый агент владельца" in _tool_fn(server, "fleet_take")("Q1-W1")
    report(json.dumps({"id": "q1-w1", "sent": True, "url": "https://chatgpt.com/c/abcdef12-1111"}))
    assert json.loads(report(json.dumps({"id": "q1-w1", "answer": "сделал, тесты зелёные"})))["new_answer"]
    with store.read() as tx:
        body = tx.one("SELECT body FROM messages WHERE to_worker='W1' ORDER BY created_at DESC LIMIT 1")["body"]
    assert "сделал, тесты зелёные" in body and "q1-w1" in body
    shown = fq("W1", "show", ["q1"])
    assert "сделал, тесты зелёные" in shown and "answered" in shown
    assert "закрыт" in fq("W1", "close", ["q1"])


def test_quick_agent_inherits_owner_role(tmp_path: Path) -> None:
    server, store, root, _ = _make_server(tmp_path)
    browser = _tool_fn(server, "fleet_browser")
    _tool_fn(server, "fleet_join")("tab-a")
    assert "только агенту с ролью владельца" in browser("W1", "list")
    quick.create(_book(store), owner="claude", prompt="x")
    console = _tool_fn(server, "fleet_owner")
    assert "роль владельца" not in console("Q1", "lock").lower() or "закрыта" in console("Q1", "lock")


def test_browser_result_split_truncates_and_keeps_images() -> None:
    from dev_mcp.fleet.owner_browser import MAX_TEXT, split_result

    text, images = split_result({"content": [{"type": "text", "text": "a" * (MAX_TEXT + 50)},
                                             {"type": "image", "data": "iVBORw0KGgo=", "mimeType": "image/png"}]})
    assert len(text) < MAX_TEXT + 200 and "обрезано" in text
    assert images and images[0][1] == "image/png"
    err, _ = split_result({"isError": True, "content": [{"type": "text", "text": "boom"}]})
    assert err.startswith("ОШИБКА")


def test_overseer_mark_from_popup(tmp_path: Path) -> None:
    server, store, _, _ = _make_server(tmp_path)
    mark = _tool_fn(server, "fleet_overseer_mark")
    chats = _tool_fn(server, "fleet_overseer_chats")
    url = "https://chatgpt.com/g/g-p-x/c/6af9a83b-a8cc-83ed-8e3d-2ed0204f5a27"
    assert json.loads(mark(url, "off"))["mode"] == "off"
    assert json.loads(chats()) == []
    assert json.loads(mark(url, "shadow"))["mode"] == "shadow"
    assert [c["mode"] for c in json.loads(chats())] == ["shadow"]
    assert json.loads(mark(url, "off"))["mode"] == "off"
    assert json.loads(chats()) == []


def test_operator_scope_opens_tree_deny_for_claude_quick_agent(tmp_path: Path) -> None:
    """Владелец 15.11: быстрый агент Claude Code со --scope получает права оператора (dev-mcp в tree_deny)."""
    import asyncio

    from fastmcp import Client

    from dev_mcp.audit import Audit
    from dev_mcp.fleet.store import Store
    from dev_mcp.policy import DELEGATED_ROOTS, Policy
    from dev_mcp.server import build_server

    ws = tmp_path / "ws"
    platform = ws / "platform"
    platform.mkdir(parents=True)
    (ws / "proj").mkdir()
    config = tmp_path / "policy.toml"
    config.write_text(
        f"roots = ['{ws}']\ntree_roots = ['{ws}']\ntree_deny = ['{platform}']\n"
        "exec_allowed_bin = ['python']\nallow_write = false\ndeny_patterns = ['.env']\n"
        "exec_timeout_cap_s = 30\n", encoding="utf-8")
    server = build_server(Policy.load(config), Audit(tmp_path / "logs" / "audit.jsonl"))
    store = Store.open(str(tmp_path / "data" / "fleet" / "fleet.sqlite"))
    book = _book(store)
    quick.create(book, owner="claude", prompt="почини платформу", scope=[str(platform)])
    quick.create(book, owner="W5", prompt="чужое", scope=[str(platform)])
    session = quick.create(book, owner="claude:7f2a91", prompt="сессия", scope=[str(platform)])
    assert session["tag"] == "Q1-7F2A91"
    assert quick.operator_roots(session) == (platform,)

    async def call(tool: str, args: dict) -> str:
        async with Client(server) as client:
            res = await client.call_tool(tool, args, raise_on_error=False)
        return "\n".join(getattr(c, "text", "") for c in res.content)

    def write(agent: str, name: str) -> str:
        return asyncio.run(call("write_file", {"path": str(platform / name), "content": "ok", "agent": agent}))

    assert "tree_deny" in write("", "a.txt")                       # без агента — закрыто
    assert "создан" in write("Q1", "x.txt"), write("Q1", "x2.txt")  # оператор — можно
    assert "создан" in write("Q1-7F2A91", "session.txt")           # новая сессия тоже оператор
    assert (platform / "x.txt").read_text(encoding="utf-8") == "ok"
    assert "tree_deny" in write("Q1-W5", "b.txt")                  # быстрый агент ведущего — нет
    ran = asyncio.run(call("run", {"root": str(platform), "argv": ["python", "-c", "print('op-run')"],
                                   "agent": "Q1"}))
    assert "op-run" in ran, ran
    assert DELEGATED_ROOTS.get() == ()                              # не утекает из вызова
    quick.close(book, "q1", owner="claude")
    assert "tree_deny" in write("Q1", "c.txt")                      # закрыл — права нет


def test_operator_scope_negative_cases(tmp_path: Path) -> None:
    """62 п.2: без scope, выход за scope (.., junction), маски секретов, изоляция параллельных вызовов."""
    import asyncio
    import os
    import subprocess

    from fastmcp import Client

    from dev_mcp.audit import Audit
    from dev_mcp.fleet.store import Store
    from dev_mcp.policy import Policy
    from dev_mcp.server import build_server

    ws = tmp_path / "ws"
    platform, other = ws / "platform", ws / "other"
    platform.mkdir(parents=True)
    other.mkdir()
    config = tmp_path / "policy.toml"
    config.write_text(
        f"roots = ['{ws}']\ntree_roots = ['{ws}']\ntree_deny = ['{platform}', '{other}']\n"
        "exec_allowed_bin = ['python']\nallow_write = false\ndeny_patterns = ['.env']\n"
        "exec_timeout_cap_s = 30\n", encoding="utf-8")
    server = build_server(Policy.load(config), Audit(tmp_path / "logs" / "audit.jsonl"))
    store = Store.open(str(tmp_path / "data" / "fleet" / "fleet.sqlite"))
    book = _book(store)
    quick.create(book, owner="claude", prompt="со scope", scope=[str(platform)])   # Q1
    quick.create(book, owner="claude", prompt="без scope")                          # Q2
    link = platform / "escape"
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(other)], check=True, capture_output=True)
    else:
        link.symlink_to(other, target_is_directory=True)

    async def write(agent: str, path: Path) -> str:
        async with Client(server) as client:
            res = await client.call_tool("write_file", {"path": str(path), "content": "x", "agent": agent},
                                         raise_on_error=False)
        return "\n".join(getattr(c, "text", "") for c in res.content)

    def w(agent: str, path: Path) -> str:
        return asyncio.run(write(agent, path))

    assert "tree_deny" in w("Q2", platform / "a.txt")                 # нет scope — нет прав
    assert "tree_deny" in w("Q1", other / "a.txt")                    # чужое поддерево
    assert "создан" not in w("Q1", platform / ".." / "other" / "b.txt")
    assert not (other / "b.txt").exists()
    assert "tree_deny" in w("Q1", link / "c.txt")                     # junction наружу
    assert not (other / "c.txt").exists()
    assert "создан" not in w("Q1", platform / ".env")                 # маска секрета сильнее
    assert not (platform / ".env").exists()

    async def both() -> list[str]:                                    # контекст не протекает
        return await asyncio.gather(write("Q1", platform / "p1.txt"), write("Q2", platform / "p2.txt"),
                                    write("Q1", platform / "p3.txt"))
    got = asyncio.run(both())
    assert "создан" in got[0] and "tree_deny" in got[1] and "создан" in got[2]
    assert not (platform / "p2.txt").exists()


def test_quick_chain_waiting_then_release_with_answers_and_closed(tmp_path: Path) -> None:
    book = quick.Book(tmp_path)
    a = quick.create(book, owner="claude", prompt="A")
    b = quick.create(book, owner="claude", prompt="B")
    c = quick.create(book, owner="claude", prompt="C", after=["q1", "q2"])
    assert c["state"] == "waiting" and c["after"] == ["q1", "q2"]
    assert {x["id"] for x in quick.orders(book)} == {"q1", "q2"}
    with pytest.raises(ValueError, match="ждёт"):
        quick.say(book, c["id"], "не открывай раньше", owner="claude")

    quick.report(book, {"id": a["id"], "answer": "X" * 7000})
    assert {x["id"] for x in quick.orders(book)} == {"q1", "q2"}
    quick.close(book, b["id"], owner="claude")
    orders = quick.orders(book)
    chained = next(x for x in orders if x["id"] == c["id"])
    assert chained["send"].count("X") == 6000
    assert "### q1 (последний ответ)\n" in chained["send"]
    assert "### q2 (последний ответ)\n(закрыт без ответа)" in chained["send"]
    assert quick.get(book, c["id"])["state"] == "new"
    assert quick.orders(book)[-1]["send"] == chained["send"]  # no duplicate context


def test_quick_chain_invalid_and_cycles(tmp_path: Path) -> None:
    book = quick.Book(tmp_path)
    a = quick.create(book, owner="claude", prompt="A")
    with pytest.raises(ValueError, match="нет"):
        quick.create(book, owner="claude", prompt="B", after=["q999"])
    with book.locked() as jobs:
        jobs[a["id"]]["after"] = ["q2"]
    with pytest.raises(ValueError, match="цикл"):
        quick.create(book, owner="claude", prompt="B", after=["q1"])
    assert list(book.jobs()) == ["q1"]


def test_quick_cli_new_after_atfile_and_wait_cursor(tmp_path: Path, monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location(
        "fleet_quick_chain_cli", Path(__file__).resolve().parents[2] / "tools" / "fleet.py")
    assert spec and spec.loader
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    db = tmp_path / "fleet.sqlite"
    # Existing register callback requires the real fleet database.
    _, store, _, _ = _make_server(tmp_path)
    db = Path(store.path)
    book = _book(store)
    a = quick.create(book, owner="claude", prompt="A")
    source = tmp_path / "multiline.txt"
    source.write_text("Первая строка\nВторая строка", encoding="utf-8")
    ns = SimpleNamespace(action="new", words=["@" + str(source)], project="", scope=[],
                         home="", after=a["id"], timeout=0, all=False)
    code, text = cli._cmd_quick(db, ns)
    assert code == 0 and "ожидает" in text
    chained = quick.get(book, "q2")
    assert chained["prompt"] == "Первая строка\nВторая строка"
    assert chained["state"] == "waiting"
    ns.action, ns.words, ns.after = "wait", [a["id"]], "0"
    quick.report(book, {"id": a["id"], "sent": True})
    quick.report(book, {"id": a["id"], "answer": "ok"})
    code, text = cli._cmd_quick(db, ns)
    assert code == 0 and "ok" in text
    assert any(x["id"] == "q2" for x in quick.orders(book))


def test_claude_quick_adopt_idle_force_and_audit(tmp_path: Path, monkeypatch) -> None:
    book = quick.Book(tmp_path)
    job = quick.create(book, owner="claude:a57a6d", prompt="handoff")
    jid = job["id"]
    assert jid == "q1-a57a6d"
    with pytest.raises(PermissionError, match="активна"):
        quick.adopt(book, jid, owner="claude:b89b10")
    with pytest.raises(PermissionError):
        quick.adopt(book, jid, owner="W3", force=True)
    with pytest.raises(PermissionError):
        quick.adopt(book, "q1", owner="claude:b89b10", force=True)
    adopted = quick.adopt(book, jid, owner="claude:b89b10", force=True)
    assert adopted["owner"] == "claude:b89b10"
    assert adopted["events"][-1]["from"] == "claude:a57a6d"
    assert adopted["events"][-1]["to"] == "claude:b89b10"
    assert adopted["events"][-1]["at"]
    assert quick.get(book, jid, owner="claude:b89b10")
    with pytest.raises(PermissionError):
        quick.get(book, jid, owner="claude:a57a6d")
    another = quick.create(book, owner="claude:c77777", prompt="idle")
    with book.locked() as jobs:
        jobs[another["id"]]["created"] = "2026-01-01T00:00:00+00:00"
        jobs[another["id"]]["updated"] = "2026-01-01T00:00:00+00:00"
    assert quick.adopt(book, another["id"], owner="claude:b89b10")["owner"] == "claude:b89b10"
