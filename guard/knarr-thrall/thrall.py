"""Thrall — edge model triage for inbound mail.

Classification using a swappable backend:
  - embedded: llama-cpp-python, CPU-only, no external dependencies (default)
  - ollama: HTTP call to ollama server (local/LAN, zero cost)
  - openai: any OpenAI-compatible API (metered, cost-budgeted)

Trust tiers:
  team    → instant wake, no LLM call
  known   → LLM classifies
  unknown → LLM classifies (higher bar for wake)

v3: multi-backend via backends.py. Adds openai support, cost tracking,
    health status. Keeps same triage() API as v2.
"""

import asyncio
import hashlib
import json
import logging
import re
import threading
import time
from typing import Any, Dict, Optional

from backends import ThrallBackend, create_backend

DEFAULT_SYSTEM_PROMPT = """You classify inbound P2P messages. Reply with exactly one JSON object.
Valid actions: drop, wake, reply.
- drop: spam, noise, single-word messages, gibberish,
        AND acknowledgments ("got it", "thanks", "received", "logged",
        "noted", "will do", "cheers") — these are terminal, no reply needed
- wake: legitimate questions, collaboration requests, technical discussions,
        explicit requests for action
- reply: simple greetings, status checks ("hello", "is your node online?")
Sender trust: {tier}. For unknown senders, prefer drop unless clearly legitimate.

Output format: {{"action":"drop"|"wake"|"reply","reason":"brief explanation"}}

Examples:
Message: "hey" -> {{"action":"drop","reason":"single word, no content"}}
Message: "Can you run digest-voice on this topic?" -> {{"action":"wake","reason":"skill request"}}
Message: "Hello, is your node online?" -> {{"action":"reply","reason":"status check greeting"}}
Message: "Thanks for the update!" -> {{"action":"drop","reason":"acknowledgment, terminal"}}
Message: "Received, logged it." -> {{"action":"drop","reason":"ack, no reply needed"}}"""


def prompt_hash(prompt_text: str) -> str:
    """SHA256 hash of prompt text, first 16 chars."""
    return hashlib.sha256(prompt_text.encode()).hexdigest()[:16]


# ── Backend management ──

_backend_cache: Optional[ThrallBackend] = None
_backend_lock = threading.Lock()


def _get_backend(config: Dict[str, Any], log: logging.Logger) -> ThrallBackend:
    """Get or create the thrall backend. Thread-safe, lazy-initialized."""
    global _backend_cache
    if _backend_cache is not None:
        return _backend_cache
    with _backend_lock:
        if _backend_cache is not None:
            return _backend_cache

        # Migrate legacy config keys to new structure
        thrall_cfg = _migrate_config(config)
        _backend_cache = create_backend(thrall_cfg)
        log.info(f"Thrall: backend={_backend_cache.name} model={_backend_cache.model_name}")
        return _backend_cache


def reset_backend():
    """Force backend re-creation on next call. Used by sentinel reload."""
    global _backend_cache
    with _backend_lock:
        _backend_cache = None


def _migrate_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Migrate v2 flat config to v3 backend structure.

    v2: backend="embedded", model_path="...", ollama_url="..."
    v3: backend="local", local={...}, ollama={...}, openai={...}
    """
    backend_name = config.get("backend", "embedded")

    if backend_name == "embedded":
        # Map "embedded" → "local" for backends.py
        return {
            "backend": "local",
            "local": {
                "model_path": config.get("model_path", "/app/models/gemma3-1b.gguf"),
                "n_threads": int(config.get("n_threads", 2)),
                "n_ctx": int(config.get("n_ctx", 1024)),
                "max_tokens": int(config.get("max_tokens", 128)),
            },
        }
    elif backend_name == "ollama":
        return {
            "backend": "ollama",
            "ollama": {
                "url": config.get("ollama_url", "http://localhost:11434"),
                "model": config.get("model", "gemma3:1b"),
                "timeout": int(config.get("timeout_seconds", 10)),
                "temperature": 0.1,
                "max_tokens": 128,
                "num_ctx": 1024,
            },
        }
    elif backend_name in ("local", "openai"):
        # Already v3 format — pass through
        return config
    else:
        # Unknown backend name — try passing through, factory will raise
        return config


def _parse_classify_result(raw_text: str) -> dict:
    """Parse raw LLM output into a dict with 'action' and 'reason'."""
    text = raw_text.strip()

    # Strip markdown code fences
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()

    # Try direct JSON parse
    try:
        result = json.loads(text)
        if isinstance(result, dict) and "action" in result:
            return result
    except (json.JSONDecodeError, TypeError):
        pass

    # Try to find JSON object in the text
    match = re.search(r'\{[^{}]*"action"\s*:\s*"[^"]*"[^{}]*\}', text)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    return {"action": "drop", "reason": f"unparseable LLM output: {text[:80]}"}


async def triage(
    from_node: str,
    body_text: str,
    msg_type: str,
    trust_tiers: Dict[str, list],
    config: Dict[str, Any],
    log: logging.Logger,
    active_prompt: Optional[str] = None,
) -> Dict[str, Any]:
    """Classify inbound mail and return a triage decision.

    Args:
        active_prompt: Override system prompt. If None, uses DEFAULT_SYSTEM_PROMPT.

    Returns:
        {"action": "drop"|"wake"|"reply", "reason": str,
         "trust_tier": str, "wall_ms": int,
         "reasoning": str, "prompt_hash": str,
         "backend": str}
    """
    t0 = time.time()
    sys_prompt = active_prompt or DEFAULT_SYSTEM_PROMPT
    p_hash = prompt_hash(sys_prompt)

    # ── Resolve trust tier ──
    tier = _resolve_tier(from_node, trust_tiers)

    # ── Team nodes: instant wake, skip LLM ──
    if tier == "team":
        wall_ms = int((time.time() - t0) * 1000)
        return {
            "action": "wake",
            "reason": "team node — bypass",
            "trust_tier": tier,
            "wall_ms": wall_ms,
            "reasoning": "team node — no classification",
            "prompt_hash": p_hash,
            "backend": "bypass",
        }

    # ── Call edge model via selected backend ──
    backend = _get_backend(config, log)
    formatted_prompt = sys_prompt.format(tier=tier)

    try:
        # Backend.infer() returns raw text; we parse it here
        raw_text = await backend.infer(formatted_prompt, body_text[:800])
        result = _parse_classify_result(raw_text)
        action = result.get("action", "").lower().strip()
        reasoning = json.dumps(result)

        if action not in ("drop", "wake", "reply"):
            log.warning(f"Thrall: bad action '{action}', falling back to tier")
            action = _tier_fallback_action(tier, config)
            reason = f"bad LLM action '{result.get('action')}', tier fallback"
        else:
            reason = result.get("reason", f"LLM classified as {action}")
    except Exception as e:
        log.warning(f"Thrall: backend failed ({e}), using fallback")
        action = _tier_fallback_action(tier, config)
        reason = f"backend error: {str(e)[:100]}, tier fallback"
        reasoning = f"error: {str(e)[:200]}"

    wall_ms = int((time.time() - t0) * 1000)
    return {
        "action": action,
        "reason": reason,
        "trust_tier": tier,
        "wall_ms": wall_ms,
        "reasoning": reasoning,
        "prompt_hash": p_hash,
        "backend": backend.name,
    }


_HEX_RE = re.compile(r'^[0-9a-f]+$')


def sanitize_node_prefix(from_node: str) -> str:
    """Extract and validate a 16-char hex prefix from a node ID.
    Returns sanitized prefix or 'invalid' if not valid hex."""
    prefix = from_node[:16].lower()
    if _HEX_RE.match(prefix):
        return prefix
    return "invalid"


def _resolve_tier(from_node: str, trust_tiers: Dict[str, list]) -> str:
    """Match node ID prefix against trust tier lists."""
    for tier_name, prefixes in trust_tiers.items():
        for prefix in prefixes:
            if from_node.startswith(prefix):
                return tier_name
    return "unknown"


def _tier_fallback_action(tier: str, config: Dict[str, Any]) -> str:
    """Static fallback when backend is unavailable."""
    fallback_mode = config.get("fallback", "tier")
    if fallback_mode == "wake":
        return "wake"
    if fallback_mode == "drop":
        return "drop"
    if tier == "team":
        return "wake"
    if tier == "known":
        return "wake"
    return "drop"
