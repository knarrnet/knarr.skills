"""Shared helpers for Batch 1 Knarr skills."""

from __future__ import annotations

import json
import os
import re
from html import unescape
from typing import Any, Dict, Iterable, Tuple

import requests

MAX_TEXT_CHARS = 180000
MAX_FIELD_CHARS = 200000
DEFAULT_TIMEOUT = 20


class SkillError(Exception):
    """Raised for expected skill-level validation failures."""


def error_result(message: str) -> Dict[str, str]:
    return {"error": truncate_text(str(message), 4000)}


def truncate_text(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 16)] + "\n...[truncated]"


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def clean_html_text(html: str) -> Tuple[str, str]:
    """Return (title, plain_text) from raw HTML using a lightweight cleaner."""
    title = ""
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    if title_match:
        title = normalize_space(unescape(re.sub(r"<[^>]+>", "", title_match.group(1))))

    # Remove script/style/noscript blocks first.
    cleaned = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"<style[^>]*>.*?</style>", " ", cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"<noscript[^>]*>.*?</noscript>", " ", cleaned, flags=re.IGNORECASE | re.DOTALL)

    # Replace block-level tags with line breaks before stripping tags.
    cleaned = re.sub(r"</?(p|div|h1|h2|h3|h4|h5|h6|li|tr|td|th|br|section|article|header|footer)[^>]*>", "\n", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    plain = normalize_space(unescape(cleaned))
    return title, truncate_text(plain)


def parse_int(raw: str, default: int, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    if min_value is not None:
        value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def parse_json_list(raw: str) -> list:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SkillError(f"Invalid JSON list: {exc}") from exc
    if not isinstance(data, list):
        raise SkillError("Expected a JSON array")
    return data


def to_json_string(data: Any, limit: int = MAX_FIELD_CHARS) -> str:
    raw = json.dumps(data, ensure_ascii=True, separators=(",", ":"))
    return truncate_text(raw, limit)


def ensure_flat_str_dict(
    data: Dict[str, Any],
    *,
    default_limit: int = MAX_FIELD_CHARS,
    per_key_limits: Dict[str, int] | None = None,
) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, value in data.items():
        key_s = str(key)
        limit = default_limit
        if per_key_limits and key_s in per_key_limits:
            try:
                limit = int(per_key_limits[key_s])
            except (TypeError, ValueError):
                limit = default_limit
            if limit < 1:
                limit = default_limit
        if isinstance(value, str):
            out[key_s] = truncate_text(value, limit)
        else:
            out[key_s] = truncate_text(str(value), limit)
    return out


def http_get(url: str, *, headers: Dict[str, str] | None = None, timeout: int = DEFAULT_TIMEOUT, max_bytes: int = 5_000_000) -> Tuple[str, str, str]:
    """Fetch URL and return (content_type, final_url, text_body)."""
    if not url:
        raise SkillError("Missing required field: url")

    req_headers = {"User-Agent": os.getenv("SKILL_HTTP_USER_AGENT", "knarr-batch1-provider/1.0")}
    if headers:
        req_headers.update(headers)

    with requests.get(url, headers=req_headers, timeout=timeout, stream=True) as resp:
        resp.raise_for_status()
        chunks = []
        total = 0
        for chunk in resp.iter_content(chunk_size=8192):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise SkillError(f"Response exceeded {max_bytes} bytes")
            chunks.append(chunk)
        body = b"".join(chunks)
        content_type = (resp.headers.get("Content-Type") or "").lower()
        encoding = resp.encoding or "utf-8"
        text = body.decode(encoding, errors="replace")
        return content_type, str(resp.url), text


def split_lines(raw: str) -> Iterable[str]:
    for line in (raw or "").splitlines():
        stripped = line.strip()
        if stripped:
            yield stripped
