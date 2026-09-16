#!/usr/bin/env python3
"""Sync Knowledge Planet topics through the official zsxq-cli.

Run this on the Mac host, not in Docker: zsxq-cli keeps OAuth credentials in
the host keychain.  Only normalized topic JSON is sent to the local app.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.zsxq import normalize_response, parse_cli_json, parse_time, response_next_end_time


DEFAULT_GROUP_ID = "28888114545551"
DEFAULT_APP = "http://127.0.0.1:8000"
SECRET_RE = re.compile(r"(?i)(authorization|cookie|token)\s*[:=]\s*[^\s,;]+")


def _safe_error(text: str) -> str:
    clean = SECRET_RE.sub(r"\1=[redacted]", text)
    return " ".join(clean.replace("\n", " ").split())[:500]


def payload_shape(value: object, depth: int = 0) -> object:
    """Return only JSON structure and list lengths; never topic text or credentials."""
    if depth >= 4:
        return type(value).__name__
    if isinstance(value, dict):
        return {str(key): payload_shape(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        first = payload_shape(value[0], depth + 1) if value else None
        return {"list_length": len(value), "first_item": first}
    return type(value).__name__


def _cli_error(payload: dict) -> RuntimeError:
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    error_type = error.get("type")
    if error_type == "auth":
        return RuntimeError(
            "zsxq-cli 无法从 macOS Keychain 读取 OAuth 登录信息。"
            "请在终端执行 `zsxq-cli auth login`，在浏览器完成授权后再执行 `zsxq-cli auth status`。"
        )
    detail = _safe_error(str(error.get("message") or payload))
    return RuntimeError(f"zsxq-cli 请求失败：{detail}")


def fetch_page(cli: str, group_id: str, count: int, end_time: str | None) -> dict:
    """Read one page via the CLI's supported group-topics command.

    Do not use ``api raw /v2/groups/<id>/topics`` here.  That browser endpoint
    is not registered by the official CLI's raw-API bridge.  ``group +topics``
    is the supported read-only command and explicitly supports ``--end-time``.
    """
    command = [cli, "group", "+topics", "--group-id", group_id, "--limit", str(count), "--json"]
    if end_time:
        command.extend(["--end-time", end_time])
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise RuntimeError("找不到 zsxq-cli，请先执行 npm install -g zsxq-cli") from exc
    payload: dict | None = None
    try:
        payload = parse_cli_json(result.stdout or result.stderr)
    except ValueError:
        pass
    if isinstance(payload, dict) and payload.get("ok") is False:
        raise _cli_error(payload)
    if result.returncode:
        detail = _safe_error(result.stderr or result.stdout or "unknown CLI error")
        raise RuntimeError(f"zsxq-cli 请求失败（可先执行 `zsxq-cli auth status`）：{detail}")
    try:
        return payload if payload is not None else parse_cli_json(result.stdout)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc


def next_end_time(topics: list[dict]) -> str | None:
    if not topics:
        return None
    oldest = min(parse_time(topic["published_at"]) for topic in topics)
    cursor = oldest - timedelta(milliseconds=1)
    return cursor.astimezone(timezone(timedelta(hours=8))).isoformat(timespec="milliseconds").replace("+08:00", "+0800")


def post_topics(app_url: str, group_id: str, scope: str, topics: list[dict]) -> dict:
    payload = json.dumps({"group_id": group_id, "scope": scope, "topics": topics}, ensure_ascii=False).encode()
    request = Request(f"{app_url.rstrip('/')}/api/topics/sync/zsxq", data=payload,
                      headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"应用接口返回 HTTP {exc.code}: {_safe_error(body)}") from exc
    except URLError as exc:
        raise RuntimeError(f"无法连接应用 {app_url}：{_safe_error(str(exc.reason))}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="同步知识星球主题到本地分析工具")
    parser.add_argument("--group-id", default=os.getenv("ZSXQ_GROUP_ID", DEFAULT_GROUP_ID))
    parser.add_argument("--scope", default=os.getenv("ZSXQ_SCOPE", "all"), choices=("all",),
                        help="官方 CLI 当前只提供主题流读取；保留该字段用于同步记录")
    parser.add_argument("--count", type=int, default=int(os.getenv("ZSXQ_PAGE_SIZE", "20")))
    parser.add_argument("--pages", type=int, default=int(os.getenv("ZSXQ_MAX_PAGES", "5")))
    parser.add_argument("--end-time", default=os.getenv("ZSXQ_END_TIME"))
    parser.add_argument("--app-url", default=os.getenv("PLANET_APP_URL", DEFAULT_APP))
    parser.add_argument("--cli", default=os.getenv("ZSXQ_CLI", "zsxq-cli"))
    parser.add_argument("--dry-run", action="store_true", help="只读取并显示数量，不写入本地应用")
    parser.add_argument("--debug", action="store_true", help="只显示上游 JSON 结构，不显示主题正文或凭据")
    args = parser.parse_args()
    if not 1 <= args.count <= 30 or not 1 <= args.pages <= 100:
        parser.error("--count 必须在 1 到 30 之间，--pages 必须在 1 到 100 之间")

    all_topics: dict[str, dict] = {}
    end_time = args.end_time
    for _ in range(args.pages):
        payload = fetch_page(args.cli, args.group_id, args.count, end_time)
        topics, has_more = normalize_response(payload, page_size=args.count)
        if args.debug:
            print(json.dumps({
                "debug": {"page": _ + 1, "payload_shape": payload_shape(payload),
                          "normalized_topics": len(topics), "has_more": has_more,
                          "has_next_end_time": bool(response_next_end_time(payload))}
            }, ensure_ascii=False))
        before = len(all_topics)
        all_topics.update({topic["topic_id"]: topic for topic in topics})
        if not topics or len(topics) < args.count or not has_more or len(all_topics) == before:
            break
        end_time = response_next_end_time(payload) or next_end_time(topics)
        if not end_time:
            break

    topics = list(all_topics.values())
    if args.dry_run:
        print(json.dumps({"topics": len(topics), "group_id": args.group_id, "scope": args.scope}, ensure_ascii=False))
        return 0
    result = post_topics(args.app_url, args.group_id, args.scope, topics)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1)
