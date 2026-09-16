"""Knowledge Planet payload normalization.

The API is called by the host-side zsxq-cli script.  This module only handles
the JSON shape returned by the official read-only API; it never deals with
cookies or access tokens.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from typing import Any


HASHTAG_RE = re.compile(r'<e[^>]+type=["\']hashtag["\'][^>]+title=["\']([^"\']+)["\'][^>]*/?>', re.I)
TAG_RE = re.compile(r"<[^>]+>")


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def clean_text(value: Any) -> str:
    text = html.unescape(_text(value))
    text = HASHTAG_RE.sub(lambda match: f"#{match.group(1)}", text)
    return TAG_RE.sub("", text).strip()


def parse_time(value: Any) -> datetime:
    raw = _text(value).strip()
    if not raw:
        return datetime.now(timezone.utc)
    if re.search(r"[+-]\d{4}$", raw):
        raw = f"{raw[:-5]}{raw[-5:-2]}:{raw[-2:]}"
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _topic_content(topic: dict[str, Any]) -> str:
    for key in ("talk", "article", "question"):
        value = topic.get(key)
        if isinstance(value, dict):
            text = value.get("text") or value.get("content") or value.get("description")
            if text:
                return clean_text(text)
    return clean_text(topic.get("text") or topic.get("content"))


def _topic_author(topic: dict[str, Any]) -> str:
    for parent in (topic.get("talk"), topic.get("article"), topic.get("question"), topic):
        if isinstance(parent, dict):
            owner = parent.get("owner") or parent.get("user")
            if isinstance(owner, dict):
                return _text(owner.get("name") or owner.get("alias"))
    return ""


def _topic_tags(topic: dict[str, Any]) -> list[str]:
    values = topic.get("tags") or topic.get("topic_tags") or []
    tags: list[str] = []
    if isinstance(values, list):
        for value in values:
            title = value.get("title") if isinstance(value, dict) else value
            title = clean_text(title)
            if title and title not in tags:
                tags.append(title)
    return tags


def normalize_topic(topic: dict[str, Any]) -> dict[str, Any]:
    topic_id = _text(topic.get("topic_id") or topic.get("topic_uid"))
    group = topic.get("group") if isinstance(topic.get("group"), dict) else {}
    title = clean_text(topic.get("title"))
    content = _topic_content(topic)
    if not title and content:
        title = content.splitlines()[0][:500]
    return {
        "topic_id": topic_id,
        "title": title[:500],
        "content": content,
        "author": _topic_author(topic)[:200],
        "published_at": parse_time(topic.get("create_time")).isoformat(),
        "source_url": f"https://wx.zsxq.com/topic/{topic_id}" if topic_id else "",
        "tags": _topic_tags(topic),
        "group_id": _text(group.get("group_id")),
        "group_name": _text(group.get("name")),
    }


TOPIC_LIST_KEYS = ("topics", "topics_brief", "items", "list")


def _response_data(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Find topic data in browser, CLI, and MCP text envelopes."""
    pending = [payload]
    while pending:
        candidate = pending.pop(0)
        for key in TOPIC_LIST_KEYS:
            values = candidate.get(key)
            if isinstance(values, list) and (
                not values or any(isinstance(value, dict) and "topic_id" in value for value in values)
            ):
                return candidate, key
        for key in ("resp_data", "data", "result"):
            nested = candidate.get(key)
            if isinstance(nested, dict):
                pending.append(nested)
        content = candidate.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or not isinstance(block.get("text"), str):
                    continue
                try:
                    nested = json.loads(block["text"])
                except json.JSONDecodeError:
                    continue
                if isinstance(nested, dict):
                    pending.append(nested)
    return payload, "topics"


def response_next_end_time(payload: dict[str, Any]) -> str | None:
    """Read the official CLI pagination cursor without parsing topic time."""
    pending = [payload]
    while pending:
        candidate = pending.pop(0)
        cursor = candidate.get("next_end_time")
        if isinstance(cursor, str) and cursor:
            return cursor
        for key in ("resp_data", "data", "result"):
            nested = candidate.get(key)
            if isinstance(nested, dict):
                pending.append(nested)
        content = candidate.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or not isinstance(block.get("text"), str):
                    continue
                try:
                    nested = json.loads(block["text"])
                except json.JSONDecodeError:
                    continue
                if isinstance(nested, dict):
                    pending.append(nested)
    return None


def normalize_response(payload: dict[str, Any], page_size: int = 20) -> tuple[list[dict[str, Any]], bool]:
    if payload.get("succeeded") is False or payload.get("ok") is False:
        raise ValueError("Knowledge Planet API returned succeeded=false")
    data, topic_key = _response_data(payload)
    raw_topics = data.get(topic_key, [])
    topics = [normalize_topic(topic) for topic in raw_topics if isinstance(topic, dict)]
    return [topic for topic in topics if topic["topic_id"]], bool(data.get("has_more", len(raw_topics) >= page_size))


def parse_cli_json(output: str) -> dict[str, Any]:
    """Parse JSON even when a CLI adds informational lines around it."""
    text = output.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("zsxq-cli did not return JSON")
        return json.loads(text[start : end + 1])
