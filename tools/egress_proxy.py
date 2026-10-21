"""Процесс исхода флота (TZ-46): `py -3 tools\\egress_proxy.py [--port 8899]`.

Читает маршруты из data/secrets/fleet-egress.txt, раз в 30 с пробует их по порядку, держит
локальный прокси на 127.0.0.1:<порт>. Состояние — data/fleet/egress-state.json (только метки
маршрутов), смены — события `egress_switch` в реестре. Нет файла маршрутов — пишет действие
владельцу в состояние и ждёт появления файла: флотский Chrome при этом ходит напрямую (PAC DIRECT).
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dev_mcp.fleet import egress  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=egress.DEFAULT_PORT)
    ap.add_argument("--routes", type=Path, default=ROOT / "data" / "secrets" / "fleet-egress.txt")
    ap.add_argument("--db", type=Path, default=ROOT / "data" / "fleet" / "fleet.sqlite")
    ap.add_argument("--state", type=Path, default=ROOT / "data" / "fleet" / "egress-state.json")
    ap.add_argument("--interval", type=float, default=30.0)
    args = ap.parse_args(argv)

    args.state.parent.mkdir(parents=True,exist_ok=True)
    server = None
    selector = None
    while True:
        try:
            routes = egress.load_routes(args.routes)
            if not routes:
                if server is not None: server.selector.current = None
                args.state.write_text(json.dumps({'route':None,'port':args.port,
                    'owner_action':egress.missing_routes_action(args.routes)},ensure_ascii=False),encoding='utf-8')
            else:
                if selector is None or routes != selector.routes:
                    selector = egress.Selector(routes,on_switch=egress.registry_switch_writer(args.db))
                selector.refresh()
                if server is None:
                    server = egress.EgressProxy(selector,args.port)
                    threading.Thread(target=server.serve_forever,daemon=True).start()
                else:
                    server.selector = selector
                egress.write_state(args.state,selector,args.port)
        except Exception as exc:
            # Permanent daemon: bad input/transient IO must not kill it. Never
            # log exception text, which could contain upstream credentials.
            if server is not None: server.selector.current = None
            try:
                args.state.write_text(json.dumps({'route':None,'port':args.port,
                    'error_type':type(exc).__name__}),encoding='utf-8')
            except OSError:
                pass
        time.sleep(max(1,args.interval))


if __name__ == "__main__":
    sys.exit(main())
