"""Один раз подключить сервер dev-mcp к официальному MCP TickTick (OAuth).

Запуск — двойной щелчок по dev-mcp\\ticktick-connect.cmd. Откроется браузер, вы нажимаете
«Разрешить» в TickTick, окно можно закрыть. Ключ ложится в data\\secrets (агентам не виден).
Повторный запуск — переподключение (например, когда истёк срок).
"""
from __future__ import annotations

import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dev_mcp.owner import ticktick_oauth as oauth  # noqa: E402

TIMEOUT_S = 600


def main() -> int:
    meta = oauth.discover()
    result: dict = {}
    state = oauth.new_state()
    verifier, challenge = oauth.pkce()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if not self.path.startswith("/callback"):
                self.send_response(404)
                self.end_headers()
                return
            try:
                result["code"] = oauth.parse_callback(self.path, state)
                text, code = "TickTick подключён к флоту. Окно можно закрыть.", 200
            except oauth.TickTickAuthError as exc:
                result["error"] = str(exc)
                text, code = "Не получилось: " + str(exc), 400
            body = f"<meta charset=utf-8><p style='font:18px sans-serif'>{text}</p>".encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
            threading.Thread(target=server.shutdown, daemon=True).start()

        def log_message(self, *args):  # no query strings (they hold the code) in the console
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}/callback"
    client_id = oauth.register(meta, redirect)
    url = oauth.authorize_url(meta, client_id, redirect, challenge, state)
    print("Открываю TickTick для разрешения доступа флоту…")
    print("Если браузер не открылся, откройте ссылку вручную:\n" + url)
    webbrowser.open(url)
    timer = threading.Timer(TIMEOUT_S, server.shutdown)
    timer.start()
    server.serve_forever()
    timer.cancel()
    if "code" not in result:
        print("Не подключено:", result.get("error", "истекло время ожидания"))
        return 1
    grant = oauth.exchange(meta, client_id, redirect, result["code"], verifier)
    oauth.save(grant)
    info = oauth.status()
    left = info.get("days_left")
    print("Готово: TickTick подключён." + (f" Ключ действует ещё {left} дн." if left else ""))
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except oauth.TickTickAuthError as exc:
        print("Не подключено:", exc)
        code = 1
    if sys.stdin and sys.stdin.isatty():
        input("Enter — закрыть окно")
    raise SystemExit(code)
