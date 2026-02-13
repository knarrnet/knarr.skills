"""Minimal shared helpers for skill-cache skills. No external dependencies."""

from __future__ import annotations

import json
from typing import Any, Dict

MAX_TEXT_CHARS = 180000
MAX_FIELD_CHARS = 200000


class SkillError(Exception):
    """Raised for expected skill-level validation failures."""


def truncate_text(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 16)] + "\n...[truncated]"


def error_result(message: str) -> Dict[str, str]:
    return {"error": truncate_text(str(message), 4000)}


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
