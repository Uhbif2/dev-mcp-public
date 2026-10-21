"""Read current library documentation from the public Context7 HTTP API.

No third-party dependencies. Public requests honor system proxies; loopback test
servers deliberately bypass them. A bearer key, when configured, is never logged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

PROJECT_ROOT = Path(__file__).resolve().parents[1]
API_BASE = "https://context7.com"
CACHE_TTL_SECONDS = 24 * 60 * 60
MAX_RESPONSE_BYTES = 2_000_000
MAX_OUTPUT_TOKENS = 8000
MIN_REMOTE_TOKENS = 1000
CHARS_PER_TOKEN = 4


class DocsError(Exception):
    """Expected CLI failure; do not show a traceback or credential."""


def _positive_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a positive integer") from exc
    if not 1 <= n <= 32000:
        raise argparse.ArgumentTypeError("must be in the range 1..32000")
    return n


def _base_url() -> tuple[str, bool]:
    base = os.environ.get("CONTEXT7_BASE_URL", API_BASE).rstrip("/")
    parts = urlsplit(base)
    is_local = parts.hostname in {"localhost", "127.0.0.1", "::1"}
    if parts.username or parts.password or parts.query or parts.fragment or parts.path:
        raise DocsError("invalid Context7 base URL")
    if not ((parts.scheme == "https" and parts.hostname == "context7.com" and parts.port is None)
            or (is_local and parts.scheme == "http")):
        raise DocsError("Context7 URL must be https://context7.com or a loopback test server")
    return base, is_local


def _api_key() -> str | None:
    path = PROJECT_ROOT / "data" / "secrets" / "context7-key.txt"
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DocsError("cannot read Context7 API key file") from exc


def _cache_dir() -> Path:
    return Path(os.environ.get("CONTEXT7_CACHE_DIR", str(PROJECT_ROOT / "data" / "cache" / "docs")))


def _cache_path(url: str, key_present: bool) -> Path:
    digest = hashlib.sha256((url + "|" + str(key_present)).encode("utf-8")).hexdigest()
    return _cache_dir() / (digest + ".json")


def _cache_read(path: Path) -> tuple[str, str] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (isinstance(data, dict) and isinstance(data.get("text"), str)
                and isinstance(data.get("content_type"), str)
                and isinstance(data.get("created"), (int, float))
                and 0 <= time.time() - data["created"] < CACHE_TTL_SECONDS):
            return data["text"], data["content_type"]
    except (OSError, ValueError, TypeError):
        pass
    return None


def _cache_write(path: Path, text: str, content_type: str) -> None:
    temp_path = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=".docs-", suffix=".tmp", delete=False) as tmp:
            temp_path = Path(tmp.name)
            json.dump({"created": time.time(), "text": text, "content_type": content_type},
                      tmp, ensure_ascii=False)
        os.replace(temp_path, path)
    except OSError as exc:
        # A read-only cache must not prevent reading public documentation.
        print(f"docs: cache unavailable ({type(exc).__name__}); continuing", file=sys.stderr)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _fetch(url: str, *, timeout: float, no_cache: bool, is_local: bool) -> tuple[str, str]:
    key = _api_key() if not is_local else None
    cache_file = _cache_path(url, key is not None)
    if not no_cache:
        cached = _cache_read(cache_file)
        if cached is not None:
            return cached

    headers = {"Accept": "application/json, text/plain", "User-Agent": "dev-mcp-docs/1"}
    if key is not None:
        headers["Authorization"] = "Bearer " + key
    opener = build_opener(ProxyHandler({})) if is_local else build_opener()
    try:
        with opener.open(Request(url, headers=headers), timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "").lower()
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise DocsError("Context7 authorization failed; check data/secrets/context7-key.txt") from None
        if exc.code == 404:
            raise DocsError("Context7 HTTP 404: library or endpoint not found") from None
        raise DocsError(f"Context7 HTTP {exc.code}") from None
    except (TimeoutError, socket.timeout) as exc:
        raise DocsError("Context7 request timed out") from exc
    except URLError as exc:
        detail = "timeout" if isinstance(exc.reason, (TimeoutError, socket.timeout)) else "network failure"
        raise DocsError(f"Context7 {detail}") from None
    except OSError as exc:
        raise DocsError(f"Context7 transport error: {type(exc).__name__}") from None

    if len(raw) > MAX_RESPONSE_BYTES:
        raise DocsError("Context7 response exceeds safety limit")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError:
        raise DocsError("Context7 response is not UTF-8") from None
    if not text.strip():
        raise DocsError("Context7 returned an empty response")
    if not no_cache:
        _cache_write(cache_file, text, content_type)
    return text, content_type


def _output(text: str, *, max_tokens: int = MAX_OUTPUT_TOKENS) -> None:
    budget = min(max_tokens, MAX_OUTPUT_TOKENS)
    limit = budget * CHARS_PER_TOKEN
    if len(text) > limit:
        suffix = f"\n[output truncated to {budget} estimated tokens]"
        text = text[: limit - len(suffix)] + suffix
    print(text)


def _library_path(value: str) -> str:
    parts = value.strip("/").split("/")
    if not parts or any(not p or p in {".", ".."} for p in parts):
        raise DocsError("library id must resemble /owner/library or /websites/library")
    if len(parts) < 2 or "?" in value or "#" in value:
        raise DocsError("invalid Context7 library id")
    return "/".join(quote(p, safe="") for p in parts)


def search(query: str, *, timeout: float, no_cache: bool) -> None:
    base, local = _base_url()
    url = f"{base}/api/v1/search?" + urlencode({"query": query})
    text, _ = _fetch(url, timeout=timeout, no_cache=no_cache, is_local=local)
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        raise DocsError("Context7 search returned invalid JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise DocsError("Context7 search returned an invalid result schema")
    if not data["results"]:
        raise DocsError(f"Context7 found no libraries for {query!r}")
    lines = []
    for result in data["results"]:
        if not isinstance(result, dict) or not result.get("id"):
            raise DocsError("Context7 search contains an invalid library")
        versions = result.get("versions")
        version = ", ".join(str(v) for v in versions[:5]) if isinstance(versions, list) and versions else "-"
        rating = f"trust={result.get('trustScore', '-')}, benchmark={result.get('benchmarkScore', '-')}"
        # Fleet run forbids leading "/" argv (absolute paths). Emit IDs suitable
        # for copy-paste into run; _library_path still accepts upstream /ids.
        cli_id = str(result["id"]).lstrip("/")
        lines.append(f"{cli_id} | {result.get('title') or '-'} | version={version} | {rating}")
    _output("\n".join(lines))


def get(library_id: str, *, topic: str | None, tokens: int, timeout: float,
        no_cache: bool) -> None:
    base, local = _base_url()
    # Context7 returns misleading HTTP 404 ("No code snippets for token
    # limit") for some libraries below ~1000 tokens. Fetch enough and
    # locally enforce the exact user-visible budget instead.
    params: dict[str, str | int] = {"tokens": max(tokens, MIN_REMOTE_TOKENS)}
    if topic:
        params["topic"] = topic
    url = f"{base}/api/v1/{_library_path(library_id)}?" + urlencode(params)
    content, content_type = _fetch(url, timeout=timeout, no_cache=no_cache, is_local=local)
    if "json" in content_type:
        try:
            parsed = json.loads(content)
        except (ValueError, TypeError):
            raise DocsError("Context7 documentation returned invalid JSON") from None
        if isinstance(parsed, dict):
            content = next((parsed[k] for k in ("content", "text", "docs")
                            if isinstance(parsed.get(k), str)), "")
        if not isinstance(content, str) or not content.strip():
            raise DocsError("Context7 documentation JSON has no text")
    _output(content, max_tokens=tokens)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read fresh Context7 library documentation")
    commands = parser.add_subparsers(dest="command", required=True)
    s = commands.add_parser("search", help="Find library IDs and version/rating metadata")
    s.add_argument("library")
    g = commands.add_parser("get", help="Get library documentation as text")
    g.add_argument("library_id")
    g.add_argument("--topic")
    g.add_argument("--tokens", type=_positive_int, default=MAX_OUTPUT_TOKENS)
    for command in (s, g):
        command.add_argument("--timeout", type=float, default=15.0)
        command.add_argument("--no-cache", action="store_true",
                             help="Skip cache reads and writes (for diagnostics)")
    args = parser.parse_args(argv)
    if not 0.05 <= args.timeout <= 60:
        parser.error("--timeout must be between 0.05 and 60 seconds")
    try:
        if args.command == "search":
            search(args.library, timeout=args.timeout, no_cache=args.no_cache)
        else:
            get(args.library_id, topic=args.topic, tokens=args.tokens,
                timeout=args.timeout, no_cache=args.no_cache)
    except DocsError as exc:
        print(f"docs: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
