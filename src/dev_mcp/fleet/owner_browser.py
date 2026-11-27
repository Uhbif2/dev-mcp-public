"""jev-browser для агента с ролью владельца (решение владельца 15.11.2026).

До 15.11 jev-browser намеренно не публиковался удалённым агентам: залогиненный браузер
владельца — не для любой вкладки ChatGPT. Владелец решил: агент, которому он сам выдал роль
owner (`fleet.cmd owner-grant <номер>`), пользуется jev-browser. Граница — та же выдача роли,
что и у пульта `fleet_owner`; без роли инструмент отказывает.

Устройство. На каждого агента — свой процесс `agent-mcp-stdio.js` (stdio JSON-RPC), то есть своя
вкладка агентского Chrome; чужие вкладки и Chrome не трогаются. Простой дольше IDLE_S —
процесс закрывается (вкладка освобождается, входы в аккаунты остаются в профиле). Секреты
по-прежнему не видны ни агенту, ни нам: `browser_secret_fill` подставляет их внутри jev-browser,
вывод уже очищен им самим. Потолок ответа шлюза — `MAX_TEXT`; снимок экрана — картинкой.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

JEV_HOME = Path(os.environ.get("DEV_MCP_JEV_BROWSER_HOME", r"C:\hub\jev-browser"))
IDLE_S = 20 * 60
CALL_TIMEOUT_S = 30.0  # < exec_sync_budget_s 35 < WORKER_LOCAL_MCP_TIMEOUT 45
INIT_TIMEOUT_S = 20.0
MAX_TEXT = 11_000
PROTOCOL = "2025-06-18"


class BrowserError(RuntimeError):
    pass


class _Session:
    def __init__(self, agent: str, command: list[str], cwd: Path) -> None:
        self.agent = agent
        self.proc = subprocess.Popen(
            command, cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.lock = threading.Lock()  # один вызов за раз: у агента одна вкладка
        self.pending: dict[int, dict[str, Any]] = {}
        self.cond = threading.Condition()
        self.next_id = 0
        self.used = time.monotonic()
        self.tools: list[dict[str, Any]] = []
        threading.Thread(target=self._read, daemon=True, name=f"jev-browser-{agent}").start()

    def alive(self) -> bool:
        return self.proc.poll() is None

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            try:
                msg = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(msg, dict) and "id" in msg and ("result" in msg or "error" in msg):
                with self.cond:
                    self.pending[int(msg["id"])] = msg
                    self.cond.notify_all()
        with self.cond:
            self.cond.notify_all()

    def _send(self, payload: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        with self.cond:
            self.next_id += 1
            rid = self.next_id
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        except OSError as exc:
            raise BrowserError(f"jev-browser не принял запрос: {exc}") from exc
        deadline = time.monotonic() + timeout
        with self.cond:
            while rid not in self.pending:
                left = deadline - time.monotonic()
                if left <= 0 or not self.alive():
                    break
                self.cond.wait(min(left, 1.0))
            msg = self.pending.pop(rid, None)
        if msg is None:
            raise BrowserError("jev-browser не ответил вовремя" if self.alive() else "процесс jev-browser завершился")
        if "error" in msg:
            err = msg["error"] or {}
            raise BrowserError(str(err.get("message") or err)[:600])
        return msg.get("result") or {}

    def start(self) -> None:
        self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                    "clientInfo": {"name": "dev-mcp-owner-browser", "version": "1"}},
                     INIT_TIMEOUT_S)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.tools = list(self.request("tools/list", {}, INIT_TIMEOUT_S).get("tools") or [])

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()  # jev-browser сам закрывает свою вкладку по концу stdin
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass


class OwnerBrowser:
    """Процессы jev-browser по агентам: создаются при первом вызове, гаснут при простое."""

    def __init__(self, home: Path = JEV_HOME, command: list[str] | None = None) -> None:
        self.home = Path(home)
        self.command = command
        self.sessions: dict[str, _Session] = {}
        self.lock = threading.Lock()

    def _command(self) -> list[str]:
        if self.command:
            return list(self.command)
        env_file = self.home / ".env"
        entry = self.home / "dist" / "agent-mcp-stdio.js"
        if not entry.is_file():
            raise BrowserError(f"jev-browser не собран: нет {entry}")
        return ["node", *( [f"--env-file={env_file}"] if env_file.is_file() else []), str(entry)]

    def reap(self, now: float | None = None) -> list[str]:
        now = time.monotonic() if now is None else now
        with self.lock:
            gone = [a for a, s in self.sessions.items() if not s.alive() or now - s.used > IDLE_S]
            dead = [self.sessions.pop(a) for a in gone]
        for s in dead:
            s.close()
        return gone

    def session(self, agent: str) -> _Session:
        self.reap()
        with self.lock:
            s = self.sessions.get(agent)
            if s is None or not s.alive():
                s = _Session(agent, self._command(), self.home)
                self.sessions[agent] = s
        try:
            with s.lock:
                if not s.tools:
                    s.start()
        except Exception:
            with self.lock:
                self.sessions.pop(agent, None)
            s.close()
            raise
        return s

    def tools(self, agent: str) -> list[dict[str, Any]]:
        return self.session(agent).tools

    def call(self, agent: str, tool: str, args: dict[str, Any], timeout: float = CALL_TIMEOUT_S) -> dict[str, Any]:
        s = self.session(agent)
        names = {t.get("name") for t in s.tools}
        if tool not in names:
            raise BrowserError(f"нет инструмента {tool!r}; есть: {', '.join(sorted(n for n in names if n))}")
        with s.lock:
            s.used = time.monotonic()
            try:
                return s.request("tools/call", {"name": tool, "arguments": args or {}}, timeout)
            finally:
                s.used = time.monotonic()
                if tool == "browser_close":
                    with self.lock:
                        self.sessions.pop(agent, None)
                    s.close()

    def close(self, agent: str | None = None) -> None:
        with self.lock:
            items = [self.sessions.pop(a) for a in list(self.sessions) if agent in (None, a)]
        for s in items:
            s.close()


def tool_catalog(tools: list[dict[str, Any]]) -> str:
    lines = ["Инструменты jev-browser (fleet_browser(agent, tool, args)); args — по схеме:"]
    for t in tools:
        schema = t.get("inputSchema") or {}
        props = schema.get("properties") or {}
        req = set(schema.get("required") or [])
        params = ", ".join(f"{k}{'' if k in req else '?'}" for k in props)
        desc = " ".join(str(t.get("description") or "").split())[:160]
        lines.append(f"- {t.get('name')}({params}) — {desc}")
    return "\n".join(lines)


def tool_schema(tools: list[dict[str, Any]], name: str) -> str:
    for t in tools:
        if t.get("name") == name:
            return json.dumps({"name": name, "description": t.get("description"),
                               "inputSchema": t.get("inputSchema")}, ensure_ascii=False)[:MAX_TEXT]
    raise BrowserError(f"нет инструмента {name!r}")


def split_result(result: dict[str, Any]) -> tuple[str, list[tuple[bytes, str]]]:
    """Текст (обрезан под шлюз) и картинки из ответа jev-browser."""
    import base64

    texts: list[str] = []
    images: list[tuple[bytes, str]] = []
    for block in result.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            texts.append(str(block.get("text") or ""))
        elif block.get("type") == "image" and block.get("data"):
            try:
                images.append((base64.b64decode(block["data"]), str(block.get("mimeType") or "image/png")))
            except (ValueError, TypeError):
                continue
    text = "\n".join(texts)
    if result.get("isError"):
        text = "ОШИБКА jev-browser: " + text
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + f"\n[…обрезано, всего {len(text)} символов: сузь запрос — ref, selector, maxChars]"
    return text, images[:2]
