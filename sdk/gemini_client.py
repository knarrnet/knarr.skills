"""Shared Gemini API client for knarr skills.

Reusable by any skill needing Gemini text generation or structured JSON output.
Includes usage tracking for cost reporting (v0.29.0+).

Models:
    MODEL_FLASH  — fast, cheap, good for template fills and drafting
    MODEL_PRO    — heavy reasoning, good for legal analysis and complex arguments

Usage (basic):

    from gemini_client import call_gemini

    text = call_gemini(api_key, "You are a translator.", "Translate: hello")

Usage (with cost tracking):

    from gemini_client import call_gemini_with_usage

    text, usage = call_gemini_with_usage(api_key, "system", "query")
    # usage = {"prompt_tokens": 42, "candidates_tokens": 100,
    #          "total_tokens": 142, "ext_cost_usd": 0.000321}

Usage (structured JSON):

    from gemini_client import call_gemini_structured

    data = call_gemini_structured(api_key, "Return JSON.", "Extract entities from: ...")
    # data = {"entities": ["Alice", "Bob"]}
"""

from __future__ import annotations

import json
from urllib.request import urlopen, Request

GEMINI_API = "https://generativelanguage.googleapis.com/v1beta/models"

# Model tiers — use MODEL_FLASH for most tasks, MODEL_PRO for heavy reasoning
MODEL_FLASH = "gemini-3-flash-preview"
MODEL_PRO = "gemini-3.1-pro-preview"
DEFAULT_MODEL = MODEL_FLASH

# Pricing per 1M tokens (USD) — {model: (input_price, output_price)}
_PRICING = {
    MODEL_FLASH: (0.50, 3.00),
    MODEL_PRO: (2.00, 12.00),
}


def call_gemini_with_usage(
    api_key: str,
    system_prompt: str,
    user_content: str,
    *,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.3,
    max_tokens: int = 8192,
    timeout: int = 60,
    thinking_level: str | None = None,
) -> tuple[str, dict]:
    """Call Gemini API. Returns (text, usage_dict) with token counts and USD cost.

    The usage_dict contains:
        prompt_tokens: int — input token count
        candidates_tokens: int — output token count
        total_tokens: int — sum of input + output
        ext_cost_usd: float — estimated USD cost based on model pricing
    """
    gen_config = {
        "temperature": temperature,
        "maxOutputTokens": max_tokens,
    }
    if thinking_level:
        gen_config["thinkingConfig"] = {"thinkingLevel": thinking_level}

    payload = json.dumps({
        "contents": [{"parts": [{"text": user_content}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "generationConfig": gen_config,
    }).encode()

    req = Request(
        f"{GEMINI_API}/{model}:generateContent?key={api_key}",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    resp = urlopen(req, timeout=timeout)
    data = json.loads(resp.read())

    candidates = data.get("candidates", [])
    if not candidates:
        raise ValueError("Gemini returned no candidates")

    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts)
    if not text.strip():
        raise ValueError("Gemini returned empty text")

    # Extract usage metadata
    usage_meta = data.get("usageMetadata", {})
    prompt_tokens = usage_meta.get("promptTokenCount", 0)
    candidates_tokens = usage_meta.get("candidatesTokenCount", 0)
    total_tokens = prompt_tokens + candidates_tokens
    in_price, out_price = _PRICING.get(model, (0.50, 3.00))
    ext_cost_usd = (prompt_tokens * in_price + candidates_tokens * out_price) / 1_000_000

    usage = {
        "prompt_tokens": prompt_tokens,
        "candidates_tokens": candidates_tokens,
        "total_tokens": total_tokens,
        "ext_cost_usd": round(ext_cost_usd, 6),
    }
    return text, usage


def call_gemini(
    api_key: str,
    system_prompt: str,
    user_content: str,
    *,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.3,
    max_tokens: int = 8192,
    timeout: int = 60,
    thinking_level: str | None = None,
) -> str:
    """Call Gemini API for text generation. Returns text string."""
    text, _ = call_gemini_with_usage(
        api_key, system_prompt, user_content,
        model=model, temperature=temperature, max_tokens=max_tokens,
        timeout=timeout, thinking_level=thinking_level,
    )
    return text


def call_gemini_structured(
    api_key: str,
    system_prompt: str,
    user_content: str,
    *,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.1,
    max_tokens: int = 8192,
    timeout: int = 60,
    thinking_level: str | None = None,
) -> dict:
    """Call Gemini API expecting JSON response. Returns parsed dict.

    The system prompt should instruct the model to output valid JSON.
    """
    text = call_gemini(
        api_key,
        system_prompt,
        user_content,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        thinking_level=thinking_level,
    )
    # Strip markdown code fences if present
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n", 1)
        if len(lines) > 1:
            cleaned = lines[1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

    return json.loads(cleaned)
