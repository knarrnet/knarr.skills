"""Thrall — edge model triage for inbound mail.

Classification using a local edge model. Two backends:
  - embedded: llama-cpp-python, CPU-only, no external dependencies (default)
  - ollama: HTTP call to ollama server (legacy fallback)

Trust tiers:
  team    → instant wake, no LLM call
  known   → LLM classifies
  unknown → LLM classifies (higher bar for wake)

v2: adds reasoning + prompt_hash to triage results, supports prompt DB override,
    ack detection in default prompt.
"""

import asyncio
import hashlib
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

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


# ── Embedded Backend (llama-cpp-python, CPU-only) ──

class EmbeddedBackend:
    """llama-cpp-python CPU-only backend. Lazy-loaded singleton (model is ~778MB, load once)."""
    _instance = None
    _lock = threading.Lock()
    _infer_lock = threading.Lock()  # serialize inference (model is not thread-safe)
    _load_failed = False  # Q-2: skip retries after failed load (requires restart)

    def __init__(self, config: Dict[str, Any]):
        self._model_path = config.get("model_path", "/app/models/gemma3-1b.gguf")
        self._n_threads = int(config.get("n_threads", 2))
        self._n_ctx = 1024
        self._max_tokens = 128

    def _ensure_loaded(self):
        if EmbeddedBackend._instance is not None:
            return
        if EmbeddedBackend._load_failed:
            raise RuntimeError("model load previously failed (restart to retry)")
        with EmbeddedBackend._lock:
            # Double-check after acquiring lock
            if EmbeddedBackend._instance is not None:
                return
            if EmbeddedBackend._load_failed:
                raise RuntimeError("model load previously failed (restart to retry)")
            try:
                from llama_cpp import Llama
                EmbeddedBackend._instance = Llama(
                    model_path=self._model_path,
                    n_gpu_layers=0,
                    n_ctx=self._n_ctx,
                    n_threads=self._n_threads,
                    verbose=False,
                )
            except Exception:
                EmbeddedBackend._load_failed = True
                raise

    def classify(self, system_prompt: str, body_text: str) -> dict:
        """Classify body_text using the given system prompt. Returns raw model output dict.
        Serialized via _infer_lock — llama-cpp model is NOT thread-safe (GPT C-1)."""
        self._ensure_loaded()
        # gemma3 chat template requires multimodal content format
        def _wrap(text):
            return [{"type": "text", "text": text}]
        with EmbeddedBackend._infer_lock:
            resp = EmbeddedBackend._instance.create_chat_completion(
                messages=[
                    {"role": "system", "content": _wrap(system_prompt)},
                    {"role": "user", "content": _wrap(body_text)},
                ],
                temperature=0.1,
                max_tokens=self._max_tokens,
                response_format={"type": "json_object"},
            )
        content = resp["choices"][0]["message"]["content"]
        return json.loads(content)


# ── Ollama Backend (legacy HTTP fallback) ──

class OllamaBackend:
    """HTTP backend calling ollama /api/chat."""
    def __init__(self, config: Dict[str, Any]):
        self._model = config.get("model", "gemma3:1b")
        self._url = config.get("ollama_url", "http://localhost:11434")
        self._timeout = int(config.get("timeout_seconds", 10))

    def classify(self, system_prompt: str, body_text: str) -> dict:
        """Classify body_text using the given system prompt. Returns raw model output dict."""
        url = f"{self._url}/api/chat"
        payload = json.dumps({
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": body_text},
            ],
            "stream": False,
            "format": "json",
            "options": {
                "num_predict": 128,
                "num_ctx": 1024,
                "temperature": 0.1,
            },
        }).encode()

        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            if resp.status != 200:
                raise RuntimeError(f"ollama {resp.status}")
            data = json.loads(resp.read(65536))  # W-3: cap response size

        content = data.get("message", {}).get("content", "")
        content = content.strip()
        if content.startswith("```"):
            lines = content.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            content = "\n".join(lines).strip()

        return json.loads(content)


# ── Backend factory ──

_backend_cache: Optional[Any] = None
_backend_lock = threading.Lock()


def _get_backend(config: Dict[str, Any], log: logging.Logger):
    global _backend_cache
    if _backend_cache is not None:
        return _backend_cache
    with _backend_lock:
        if _backend_cache is not None:
            return _backend_cache
        backend_name = config.get("backend", "ollama")
        if backend_name == "embedded":
            log.info("Thrall: initializing embedded backend (llama-cpp-python)")
            _backend_cache = EmbeddedBackend(config)
        else:
            log.info(f"Thrall: using ollama backend at {config.get('ollama_url', 'localhost')}")
            _backend_cache = OllamaBackend(config)
        return _backend_cache


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
         "reasoning": str, "prompt_hash": str}
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
        }

    # ── Call edge model via selected backend ──
    backend = _get_backend(config, log)
    formatted_prompt = sys_prompt.format(tier=tier)

    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, backend.classify, formatted_prompt, body_text[:800]
        )
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
