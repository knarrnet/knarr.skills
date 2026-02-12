"""Public TTS facade: routes to the best available engine with GPU balancing."""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict

from _common import SkillError, ensure_flat_str_dict, error_result, truncate_text

NODE: Any = None

# Engine priority order (tried in sequence if GPU unavailable)
ENGINE_SKILLS = {
    "qwen3": "tts-qwen3-lite",
    "chatterbox": "tts-chatterbox-lite",
    "cosyvoice": "tts-cosyvoice-lite",
    "gptsovits": "tts-gptsovits-lite",
}

DEFAULT_ENGINE_ORDER = ["qwen3", "chatterbox", "cosyvoice", "gptsovits"]


def set_node(node: Any) -> None:
    global NODE
    NODE = node


async def handle(input_data: dict, ctx=None) -> dict:
    try:
        if NODE is None:
            raise SkillError("Skill not initialized with node context")

        text = str(input_data.get("text") or "").strip()
        if not text:
            raise SkillError("Missing required field: text")

        engine = str(input_data.get("engine") or "").strip().lower()
        fallback = str(input_data.get("fallback") or "true").strip().lower() in {"true", "1", "yes"}

        # Build engine attempt order
        if engine and engine in ENGINE_SKILLS:
            if fallback:
                order = [engine] + [e for e in DEFAULT_ENGINE_ORDER if e != engine]
            else:
                order = [engine]
        else:
            order = list(DEFAULT_ENGINE_ORDER)

        # Forward all input fields to the engine skill
        forward_keys = {
            "text", "voice", "voice_ref_asset", "voice_ref_base64", "voice_ref_text",
            "voice_ref_lang", "text_lang", "model", "response_format", "media_type",
            "speed", "temperature", "exaggeration", "cfg_weight",
            "shutdown_mode", "vram_mb", "generate_timeout_secs", "health_timeout_secs",
        }
        forward: Dict[str, str] = {}
        for key in forward_keys:
            val = input_data.get(key)
            if val is not None:
                forward[key] = str(val)

        last_error = ""
        for eng in order:
            skill_name = ENGINE_SKILLS.get(eng)
            if not skill_name:
                continue

            try:
                result = await NODE.call_local(skill_name, forward)
            except Exception as exc:
                last_error = f"{eng}: {exc}"
                continue

            status = str(result.get("status") or "")
            if status == "gpu_unavailable":
                last_error = f"{eng}: GPU unavailable - {result.get('reason', '')}"
                continue

            if "error" in result:
                last_error = f"{eng}: {result.get('error', 'unknown')}"
                continue

            # Success — pass through result with engine info
            result["routed_engine"] = eng
            return ensure_flat_str_dict(result)

        raise SkillError(f"All engines failed. Last: {truncate_text(last_error, 800)}")

    except Exception as exc:
        return error_result(str(exc))
