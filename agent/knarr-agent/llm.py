"""LLM backend abstraction: Ollama, Gemini, LlamaCpp, Static."""

import asyncio
import json
import logging
import time
from typing import Any, Dict, Optional
from urllib.request import urlopen, Request
from urllib.error import URLError

log = logging.getLogger(__name__)


def _parse_json_action(text: str) -> Dict[str, Any]:
    """Extract first JSON object from LLM output. Falls back to {"action": "log"}."""
    text = text.strip()
    # Strip <think>...</think> blocks (qwen3 reasoning)
    import re
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

    # Try direct parse
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "action" in obj:
            return obj
    except json.JSONDecodeError:
        pass

    # Scan for first { ... } block
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    obj = json.loads(text[start:i+1])
                    if isinstance(obj, dict) and "action" in obj:
                        return obj
                except json.JSONDecodeError:
                    pass
                start = -1

    log.warning(f"Could not parse LLM JSON, falling back to log: {text[:200]}")
    return {"action": "log", "summary": f"Unparseable LLM output: {text[:200]}"}


class LLMBackend:
    async def generate(self, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
        """Returns parsed JSON action dict."""
        raise NotImplementedError


class StaticBackend(LLMBackend):
    """No LLM — always returns log action. For testing and CPU-only edge nodes."""

    async def generate(self, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
        return {"action": "log", "summary": "Static backend — event recorded, no LLM decision."}


class OllamaBackend(LLMBackend):
    def __init__(self, config: Dict[str, Any]):
        self._base_url = config.get("base_url", "http://localhost:11434").rstrip("/")
        self._model = config.get("model", "qwen3:1.7b")
        self._temperature = float(config.get("temperature", 0.2))
        self._num_predict = int(config.get("num_predict", 500))
        self._num_ctx = int(config.get("num_ctx", 4096))
        self._timeout = int(config.get("timeout", 120))

    async def generate(self, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
        def _call():
            payload = json.dumps({
                "model": self._model,
                "stream": False,
                "format": "json",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "options": {
                    "temperature": self._temperature,
                    "num_predict": self._num_predict,
                    "num_ctx": self._num_ctx,
                },
            }).encode()

            req = Request(
                f"{self._base_url}/api/chat",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            resp = urlopen(req, timeout=self._timeout)
            data = json.loads(resp.read())
            content = (data.get("message") or {}).get("content", "")
            return _parse_json_action(content)

        return await asyncio.to_thread(_call)


class LlamaCppBackend(LLMBackend):
    """Local GGUF model via llama-cpp-python. CPU or GPU, no server needed."""

    def __init__(self, config: Dict[str, Any]):
        self._model_path = config.get("model_path", "")
        self._n_gpu_layers = int(config.get("n_gpu_layers", 0))  # 0=CPU, -1=all GPU
        self._n_ctx = int(config.get("n_ctx", 2048))
        self._n_threads = int(config.get("n_threads", 4))
        self._temperature = float(config.get("temperature", 0.2))
        self._max_tokens = int(config.get("max_tokens", 512))
        self._llm = None
        self._load_time = 0.0

    def _ensure_loaded(self):
        """Lazy-load the model on first call."""
        if self._llm is not None:
            return
        t0 = time.time()
        try:
            from llama_cpp import Llama
        except ImportError:
            raise RuntimeError("llama-cpp-python not installed. pip install llama-cpp-python")

        if not self._model_path:
            raise RuntimeError("llama_cpp.model_path not set in plugin.toml")

        self._llm = Llama(
            model_path=self._model_path,
            n_gpu_layers=self._n_gpu_layers,
            n_ctx=self._n_ctx,
            n_threads=self._n_threads,
            verbose=False,
        )
        self._load_time = time.time() - t0
        log.info(f"LlamaCpp model loaded in {self._load_time:.1f}s: {self._model_path}")

    async def generate(self, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
        def _call():
            self._ensure_loaded()
            t0 = time.time()

            response = self._llm.create_chat_completion(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                response_format={"type": "json_object"},
            )

            content = response["choices"][0]["message"]["content"] or ""
            elapsed = time.time() - t0
            usage = response.get("usage", {})
            tokens = usage.get("completion_tokens", 0)
            log.info(f"LlamaCpp: {tokens} tokens in {elapsed:.1f}s ({tokens/max(elapsed,0.01):.0f} tok/s)")

            return _parse_json_action(content)

        return await asyncio.to_thread(_call)


class GeminiBackend(LLMBackend):
    def __init__(self, config: Dict[str, Any], api_key: str):
        self._model = config.get("model", "gemini-3-flash-preview")
        self._temperature = float(config.get("temperature", 0.3))
        self._max_tokens = int(config.get("max_tokens", 1024))
        self._api_key = api_key

    async def generate(self, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
        def _call():
            api_url = "https://generativelanguage.googleapis.com/v1beta/models"
            payload = json.dumps({
                "contents": [{"parts": [{"text": user_prompt}]}],
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "generationConfig": {
                    "temperature": self._temperature,
                    "maxOutputTokens": self._max_tokens,
                    "responseMimeType": "application/json",
                },
            }).encode()

            req = Request(
                f"{api_url}/{self._model}:generateContent?key={self._api_key}",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            resp = urlopen(req, timeout=60)
            data = json.loads(resp.read())
            candidates = data.get("candidates", [])
            if not candidates:
                return {"action": "log", "summary": "Gemini returned no candidates"}
            content = candidates[0].get("content", {})
            parts = content.get("parts", [])
            text = parts[0].get("text", "") if parts else ""
            return _parse_json_action(text)

        return await asyncio.to_thread(_call)


def create_backend(config: Dict[str, Any], vault_get=None) -> LLMBackend:
    """Factory: create the configured LLM backend."""
    backend_name = config.get("llm_backend", "static")

    if backend_name == "ollama":
        return OllamaBackend(config.get("ollama", {}))

    if backend_name == "llama_cpp":
        return LlamaCppBackend(config.get("llama_cpp", {}))

    if backend_name == "gemini":
        api_key = ""
        if vault_get:
            try:
                api_key = vault_get("gemini_api_key") or ""
            except Exception:
                pass
        if not api_key:
            log.warning("Gemini backend selected but no API key in vault — falling back to static")
            return StaticBackend()
        return GeminiBackend(config.get("gemini", {}), api_key)

    return StaticBackend()
