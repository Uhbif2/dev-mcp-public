"""Realtime dashboard for dev-mcp audit.jsonl. Stdlib only; does not touch the server."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_LOG = REPO / "logs" / "audit.jsonl"

def parse_ts(raw: str) -> float:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except Exception:
        return time.time()

def fmt_ms(ms: int) -> str:
    if ms >= 1000:
        return f"{ms/1000:.1f}s"
    return f"{ms}ms"

def p95(values):
    if not values:
        return 0
    vals = sorted(values)
    return vals[min(len(vals)-1, max(0, int(len(vals)*0.95)-1))]

def load_tail(path: Path, bytes_back: int = 4_000_000):
    if not path.exists():
        return [], 0
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        end = fh.tell()
        start = max(0, end - bytes_back)
        fh.seek(start)
        if start:
            fh.readline()
        rows = []
        for raw in fh:
            try:
                rows.append(json.loads(raw.decode("utf-8", "replace")))
            except Exception:
                pass
        return rows, end

def render(events, started_at, path):
    now = time.time()
    e10 = [e for e in events if now - e["_t"] <= 10]
    e60 = [e for e in events if now - e["_t"] <= 60]
    durs = [int(e.get("dur_ms") or 0) for e in e60]
    agents = {str(e.get("agent") or "") for e in e60 if e.get("agent")}
    tools = Counter(str(e.get("tool") or "?") for e in e60)
    decisions = Counter(str(e.get("decision") or "?") for e in e60)
    result_bytes = sum(int(e.get("result_bytes") or 0) for e in e60)
    os.system("cls" if os.name == "nt" else "clear")
    print("DEV-MCP REALTIME  |  Ctrl+C = exit")
    print(f"log: {path}")
    print("-" * 100)
    print(
        f"calls: {len(e10):>4}/10s  {len(e60):>4}/60s  "
        f"rate={len(e60)/60:.2f}/s   agents={len(agents)}   "
        f"allow={decisions.get('allow',0)} deny={decisions.get('deny',0)}"
    )
    avg = int(sum(durs)/len(durs)) if durs else 0
    print(
        f"latency 60s: avg={fmt_ms(avg)} p95={fmt_ms(p95(durs))} max={fmt_ms(max(durs) if durs else 0)}   "
        f"result={result_bytes/1024:.1f} KiB"
    )
    if tools:
        top = "  ".join(f"{name}:{count}" for name, count in tools.most_common(10))
        print("tools 60s: " + top)
    else:
        print("tools 60s: —")
    if agents:
        print("agents 60s: " + ", ".join(sorted(agents)))
    else:
        print("agents 60s: —")
    print("-" * 100)
    print("LAST CALLS")
    print(f"{'time':8} {'decision':7} {'dur':>8} {'agent':8} {'tool':22} reason")
    for e in list(events)[-18:]:
        stamp = datetime.fromtimestamp(e["_t"], timezone.utc).astimezone().strftime("%H:%M:%S")
        decision = str(e.get("decision") or "?")[:7]
        dur = fmt_ms(int(e.get("dur_ms") or 0))
        agent = str(e.get("agent") or "-")[:8]
        tool = str(e.get("tool") or "?")[:22]
        reason = str(e.get("reason") or "").replace("\n", " ")[:45]
        print(f"{stamp:8} {decision:7} {dur:>8} {agent:8} {tool:22} {reason}")
    print("-" * 100)
    print(f"monitor uptime: {int(now-started_at)}s   updated: {datetime.now().strftime('%H:%M:%S')}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=str(DEFAULT_LOG))
    ap.add_argument("--refresh", type=float, default=1.0)
    ns = ap.parse_args()
    path = Path(ns.log)
    rows, pos = load_tail(path)
    events = deque(maxlen=10000)
    cutoff = time.time() - 3600
    for e in rows:
        e["_t"] = parse_ts(str(e.get("ts") or ""))
        if e["_t"] >= cutoff:
            events.append(e)
    started = time.time()
    next_render = 0.0
    try:
        while True:
            if path.exists():
                size = path.stat().st_size
                if size < pos:
                    pos = 0
                if size > pos:
                    with path.open("rb") as fh:
                        fh.seek(pos)
                        for raw in fh:
                            try:
                                e = json.loads(raw.decode("utf-8", "replace"))
                                e["_t"] = parse_ts(str(e.get("ts") or ""))
                                events.append(e)
                            except Exception:
                                pass
                        pos = fh.tell()
            now = time.time()
            if now >= next_render:
                render(events, started, path)
                next_render = now + max(0.2, ns.refresh)
            time.sleep(0.15)
    except KeyboardInterrupt:
        print("\nmonitor stopped")

if __name__ == "__main__":
    main()
