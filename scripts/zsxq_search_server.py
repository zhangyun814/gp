#!/usr/bin/env python3
"""Host-side search proxy for the Knowledge Planet home-page card.

Runs on the Mac host (not in Docker): zsxq-cli keeps its OAuth credentials in
the host keychain.  The web page calls http://127.0.0.1:8765/search, this
server shells out to the official read-only CLI search command, filters the
hits locally so all keywords must appear, and returns sanitized JSON.

Read-only: nothing is written to the app database or the planet.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.zsxq import parse_cli_json  # noqa: E402

CLI = os.environ.get("ZSXQ_CLI", "/usr/local/bin/zsxq-cli")
GROUP_ID = os.environ.get("ZSXQ_GROUP_ID", "28888114545551")
PORT = int(os.environ.get("ZSXQ_SEARCH_PORT", "8765"))
SEARCH_TIMEOUT = 60
CONTENT_LIMIT = 500

# Same redaction idea as scripts/sync_zsxq.py: never echo credentials back.
SECRET_RE = re.compile(r"(?i)(authorization|cookie|token)\s*[:=]\s*[^\s,;]+")
SECRET_FIELDS = {"avatar_url", "token", "cookies"}


def safe_error(text: str) -> str:
    return SECRET_RE.sub(r"\1=[redacted]", " ".join(text.replace("\n", " ").split()))[:300]


def search_cli(query: str) -> dict:
    command = [CLI, "topic", "+search", "--group-id", GROUP_ID,
               "--query", query, "--json"]
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                check=False, timeout=SEARCH_TIMEOUT)
    except FileNotFoundError:
        raise RuntimeError("找不到 zsxq-cli，请在主机执行 npm install -g zsxq-cli")
    except subprocess.TimeoutExpired:
        raise RuntimeError("星球搜索超时，请稍后重试")
    payload: dict | None = None
    try:
        payload = parse_cli_json(result.stdout or result.stderr)
    except ValueError:
        pass
    if isinstance(payload, dict) and payload.get("success") is False:
        raise RuntimeError(safe_error(str(payload.get("error") or payload)))
    if not isinstance(payload, dict):
        raise RuntimeError(safe_error(result.stderr or "星球搜索返回无法解析"))
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    if error.get("type") == "auth":
        raise RuntimeError("zsxq-cli 登录已失效，请在主机执行 zsxq-cli auth login")
    return payload


def filter_hits(payload: dict, terms: list[str], mode: str, limit: int) -> list[dict]:
    items = payload.get("topics_brief") or []
    matched: list[dict] = []
    for topic in items:
        text = f"{topic.get('title') or ''} {topic.get('content') or ''}"
        if mode == "or":
            hit = any(term in text for term in terms)
        else:
            hit = all(term in text for term in terms)
        if not hit:
            continue
        owner = topic.get("owner") if isinstance(topic.get("owner"), dict) else {}
        matched.append({
            "topic_id": topic.get("topic_id"),
            "title": topic.get("title") or "",
            "content": (topic.get("content") or "")[:CONTENT_LIMIT],
            "author": owner.get("alias") or owner.get("name") or "",
            "create_time": topic.get("create_time") or "",
            "url": f"https://wx.zsxq.com/topic/{topic.get('topic_id')}",
        })
        if len(matched) >= limit:
            break
    return matched


class SearchHandler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(204, {})

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/search":
            self._send(404, {"error": "only /search is available"})
            return
        params = urllib.parse.parse_qs(parsed.query)
        query = (params.get("q") or [""])[0].strip()
        mode = (params.get("mode") or ["and"])[0].lower()
        try:
            limit = int((params.get("limit") or ["20"])[0])
        except ValueError:
            limit = 20
        limit = max(1, min(limit, 100))
        if not query:
            self._send(400, {"error": "缺少搜索关键词 q"})
            return
        if mode not in {"and", "or"}:
            mode = "and"
        terms = [term for term in query.split() if term] or [query]
        try:
            payload = search_cli(query)
        except RuntimeError as exc:
            self._send(502, {"error": str(exc)})
            return
        self._send(200, {"total": len(filter_hits(payload, terms, mode, limit)),
                         "items": filter_hits(payload, terms, mode, limit)})

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[zsxq-search] %s\n" % (fmt % args))


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", PORT), SearchHandler)
    print(f"zsxq search proxy listening on http://127.0.0.1:{PORT} (group {GROUP_ID})",
          flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
