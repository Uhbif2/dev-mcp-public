"""Live optional relay benchmark; never reads keys.txt or proxy.env."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import statistics
import struct
import sys
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dev_mcp.fleet import gemini


def one_pixel_png() -> bytes:
    """A real 1x1 RGB PNG built with stdlib, not a disguised text payload."""
    def chunk(name: bytes, payload: bytes) -> bytes:
        body = name + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xffffffff)
    return (bytes.fromhex("89504e470d0a1a0a")
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\x10\x20\x30"))
            + chunk(b"IEND", b""))


def run(n: int, timeout: float = 20) -> dict:
    result: dict = {
        "requests_per_kind": n,
        "endpoint": "local-relay",
        "pool_key_count": None,
        "pool_key_count_note": "relay does not expose a count via GET /v1beta/models or response headers",
        "models_http_status": None,
    }
    if not gemini.ensure_relay():
        result.update({"error": (gemini.last_error() or {}).get("code", "relay_unavailable")})
        return result
    # This endpoint currently proxies Google's model list; it does not expose pool cardinality.
    request = __import__("urllib.request", fromlist=["Request"]).Request(
        gemini.BASE_URL.rstrip("/") + "/models", method="GET",
    )
    try:
        with gemini._opener.open(request, timeout=timeout) as response:
            result["models_http_status"] = response.status
            hint = response.headers.get("x-gemini-relay-key-count")
            if hint and hint.isdecimal():
                result["pool_key_count"] = int(hint)
                result["pool_key_count_note"] = "relay response header"
            response.read(1024)
    except urllib.error.HTTPError as exc:
        result["models_http_status"] = exc.code
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        result["models_error"] = type(exc).__name__

    png = one_pixel_png()
    def call(kind: str) -> dict:
        started = time.monotonic()
        reply = gemini.ask(
            "Reply with exactly OK.", images=(png,) if kind == "image" else (),
            timeout=timeout,
        )
        elapsed = (time.monotonic() - started) * 1000
        code = (gemini.last_error() or {}).get("code")
        return {"kind": kind, "ok": reply is not None, "code": code,
                "latency_ms": round(elapsed, 1)}

    with ThreadPoolExecutor(max_workers=min(2 * n, 20)) as pool:
        futures = [pool.submit(call, kind) for kind in (["short"] * n + ["image"] * n)]
        calls = [f.result() for f in futures]
    result["total"] = len(calls)
    result["successful"] = sum(v["ok"] for v in calls)
    result["rate_429"] = round(sum(v["code"] == "rate_limited" for v in calls) / len(calls), 4)
    result["median_latency_ms"] = round(statistics.median(v["latency_ms"] for v in calls), 1)
    result["short"] = {"successful": sum(v["ok"] for v in calls if v["kind"] == "short")}
    result["image"] = {"successful": sum(v["ok"] for v in calls if v["kind"] == "image")}
    result["error_codes"] = {key: sum(v["code"] == key for v in calls)
                             for key in sorted({v["code"] for v in calls if v["code"]})}
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Live benchmark of local Gemini relay")
    parser.add_argument("--n", type=int, default=1, help="parallel short and image requests each")
    parser.add_argument("--timeout", type=float, default=20)
    args = parser.parse_args()
    if args.n < 1 or args.n > 30 or args.timeout <= 0:
        parser.error("n must be 1..30 and timeout > 0")
    # A one-shot benchmark must not leave a relay process that it launched.
    try:
        result = run(args.n, args.timeout)
        print(json.dumps(result, ensure_ascii=False))
    finally:
        proc = gemini._spawned_relay
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except __import__("subprocess").TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)


if __name__ == "__main__":
    main()
