# Cross-Model Code Review Prompt

Use this prompt with GPT-4 or Gemini. Paste all three source files after the prompt.

---

## Prompt

You are reviewing a Python plugin for a P2P agent network (knarr). The plugin is called "thrall" â€” an edge classifier that intercepts inbound messages and classifies them using a local small LLM (gemma3:1b, 778MB GGUF, CPU-only).

**Architecture context:**
- The plugin runs inside an asyncio event loop (single-threaded for async code)
- LLM inference runs in a ThreadPoolExecutor via `loop.run_in_executor()`
- The plugin has its own SQLite DB (`thrall.db`), separate from the node's DB
- The DB connection is synchronous sqlite3 (no async wrapper)
- The admin skill module shares the same DB connection object (passed by reference)
- Breaker files are JSON files on disk in a `breakers/` subdirectory
- The plugin does NOT send replies â€” it only classifies, records, and blocks

**Review focus areas:**

1. **Thread safety**: The LLM backend runs in a thread pool. The DB and in-memory dicts are accessed from the event loop. Is there a thread-safety gap where the executor thread could touch shared state?

2. **SQLite correctness**: Single connection, WAL mode, synchronous calls on the event loop. Any risk of blocking the event loop for too long? Any risk of corruption from concurrent access?

3. **Security**:
   - Can `from_node` (network-controlled input) be used for SQL injection, path traversal, or log injection?
   - Can a malicious node craft a message that causes the classifier to behave unexpectedly?
   - Can the prompt-load skill be abused if the whitelist is misconfigured?

4. **Memory**: OrderedDict with LRU eviction for reply_counter and solicited_sends. Plain dict for rate_limit. Any leak paths?

5. **Edge cases**:
   - What happens if thrall.db is deleted while the plugin is running?
   - What happens if a breaker file contains invalid JSON?
   - What happens if the LLM returns malformed JSON?
   - What happens if `on_shutdown` races with an in-flight classification?

6. **Correctness**:
   - Loop detection: Does the threshold logic work correctly? (threshold=2, fires on 3rd message)
   - Solicited detection: `record_send()` is a public method but must be called by the responder plugin. If it's never called, is the fallback behavior safe?
   - Ack detection in the prompt: Will gemma3:1b reliably classify "thanks for the update" as drop?

**Report format:**

```
## CRITICAL (must fix before deployment)
- [C-N] description, file:line

## WARNING (should fix, not blocking)
- [W-N] description, file:line

## GOOD (things done well)
- [G-N] description

## QUESTIONS (things to verify)
- [Q-N] question
```

---

## Files to review

Paste the contents of these three files below:
1. `thrall.py` (classification engine, ~275 lines)
2. `handler.py` (plugin hooks, ~623 lines)
3. `thrall_admin.py` (prompt management, ~121 lines)


---

### File 1: thrall.py

```python
"""Thrall â€” edge model triage for inbound mail.

Classification using a local edge model. Two backends:
  - embedded: llama-cpp-python, CPU-only, no external dependencies (default)
  - ollama: HTTP call to ollama server (legacy fallback)

Trust tiers:
  team    â†’ instant wake, no LLM call
  known   â†’ LLM classifies
  unknown â†’ LLM classifies (higher bar for wake)

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
        "noted", "will do", "cheers") â€” these are terminal, no reply needed
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


# â”€â”€ Embedded Backend (llama-cpp-python, CPU-only) â”€â”€

class EmbeddedBackend:
    """llama-cpp-python CPU-only backend. Lazy-loaded singleton (model is ~778MB, load once)."""
    _instance = None
    _lock = threading.Lock()

    def __init__(self, config: Dict[str, Any]):
        self._model_path = config.get("model_path", "/app/models/gemma3-1b.gguf")
        self._n_threads = int(config.get("n_threads", 2))
        self._n_ctx = 1024
        self._max_tokens = 128

    def _ensure_loaded(self):
        if EmbeddedBackend._instance is not None:
            return
        with EmbeddedBackend._lock:
            # Double-check after acquiring lock
            if EmbeddedBackend._instance is not None:
                return
            from llama_cpp import Llama
            EmbeddedBackend._instance = Llama(
                model_path=self._model_path,
                n_gpu_layers=0,
                n_ctx=self._n_ctx,
                n_threads=self._n_threads,
                verbose=False,
            )

    def classify(self, system_prompt: str, body_text: str) -> dict:
        """Classify body_text using the given system prompt. Returns raw model output dict."""
        self._ensure_loaded()
        # gemma3 chat template requires multimodal content format
        def _wrap(text):
            return [{"type": "text", "text": text}]
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


# â”€â”€ Ollama Backend (legacy HTTP fallback) â”€â”€

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
            data = json.loads(resp.read())

        content = data.get("message", {}).get("content", "")
        content = content.strip()
        if content.startswith("```"):
            lines = content.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            content = "\n".join(lines).strip()

        return json.loads(content)


# â”€â”€ Backend factory â”€â”€

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

    # â”€â”€ Resolve trust tier â”€â”€
    tier = _resolve_tier(from_node, trust_tiers)

    # â”€â”€ Team nodes: instant wake, skip LLM â”€â”€
    if tier == "team":
        wall_ms = int((time.time() - t0) * 1000)
        return {
            "action": "wake",
            "reason": "team node â€” bypass",
            "trust_tier": tier,
            "wall_ms": wall_ms,
            "reasoning": "team node â€” no classification",
            "prompt_hash": p_hash,
        }

    # â”€â”€ Call edge model via selected backend â”€â”€
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

```

### File 2: handler.py

```python
"""knarr-thrall: Edge classification guard plugin.

Intercepts inbound mail via on_mail_received hook. Classifies using a local
small model (gemma3:1b). Records every decision. Detects loops. Trips granular
breakers. Wakes the agent when something needs attention.

v2: transparent classification, granular breakers, loop detection, prompt security.

DB: own thrall.db in plugin_dir (synchronous sqlite3, NOT node.db).
    Single connection shared with thrall_admin via reference (not path).
    All DB writes happen on the asyncio event loop thread (single-threaded).
    thrall_admin.reload_prompt is called from the event loop via ctx callback.
"""

import importlib
import importlib.util
import json
import os
import sqlite3
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from knarr.dht.plugins import PluginHooks, PluginContext, NodeHealth
from knarr.core.models import NodeInfo

# Load thrall classifier from same directory
_thrall_spec = importlib.util.spec_from_file_location(
    "thrall", os.path.join(os.path.dirname(__file__), "thrall.py"))
thrall_mod = importlib.util.module_from_spec(_thrall_spec)
_thrall_spec.loader.exec_module(thrall_mod)

# Load admin module for prompt management
_admin_spec = importlib.util.spec_from_file_location(
    "thrall_admin", os.path.join(os.path.dirname(__file__), "thrall_admin.py"))
thrall_admin_mod = importlib.util.module_from_spec(_admin_spec)
_admin_spec.loader.exec_module(thrall_admin_mod)

# Defaults
_DEFAULT_TTL_DAYS = 30
_DEFAULT_LOOP_THRESHOLD = 2
_DEFAULT_LOOP_THRESHOLD_SESSIONLESS = 5
_DEFAULT_KNOCK_THRESHOLD = 10
_MAX_COUNTER_ENTRIES = 10_000
_REPLY_WINDOW_SECONDS = 1800  # 30 minutes
_PRUNE_INTERVAL_SECONDS = 3600  # 1 hour
_MAX_BODY_PREVIEW = 2000  # max chars from body before json.dumps fallback


class ThrallGuard(PluginHooks):
    def __init__(self, ctx: PluginContext, config: Dict[str, Any]):
        self._ctx = ctx
        self._config = config
        self._log = ctx.log
        self._enabled = config.get("enabled", True)

        if not self._enabled:
            self._log.info("Thrall guard disabled by config")
            return

        # Thrall triage config
        thrall_cfg = config.get("thrall", {})
        self._thrall_enabled = thrall_cfg.get("enabled", False)
        self._thrall_config = thrall_cfg
        self._trust_tiers = thrall_cfg.get("trust_tiers", {})

        # Ignored message types
        self._ignore_msg_types: List[str] = config.get(
            "ignore_msg_types", ["ack", "delivery", "system"])

        # Rate limiter: {node_prefix: [timestamp, ...]}
        self._rate_limit: Dict[str, List[float]] = {}
        self._max_per_hour = int(config.get("max_replies_per_hour_per_node", 5))

        # Loop detection
        self._loop_threshold = int(
            thrall_cfg.get("loop_threshold", _DEFAULT_LOOP_THRESHOLD))
        self._loop_threshold_sessionless = int(
            thrall_cfg.get("loop_threshold_sessionless",
                           _DEFAULT_LOOP_THRESHOLD_SESSIONLESS))
        self._knock_threshold = int(
            thrall_cfg.get("knock_threshold", _DEFAULT_KNOCK_THRESHOLD))

        # Reply counter: OrderedDict for LRU eviction
        # Key: (session_id_or_default, node_prefix)  Value: [timestamp, ...]
        self._reply_counter: OrderedDict[Tuple[str, str], List[float]] = OrderedDict()

        # Solicited tracking: set of (node_prefix, session_id) we've sent to
        # Populated by record_send() â€” must be called by the responder plugin
        # when it sends a reply, otherwise _is_solicited always returns False
        # and all replies use the base threshold.
        self._solicited_sends: OrderedDict[Tuple[str, str], float] = OrderedDict()

        # Classification TTL
        self._classification_ttl_seconds = int(
            thrall_cfg.get("classification_ttl_days", _DEFAULT_TTL_DAYS)) * 86400

        # Breaker directory
        self._breaker_dir = ctx.plugin_dir / "breakers"

        # Log file
        self._log_path = ctx.plugin_dir / "thrall.log"

        # Pruning timestamp
        self._last_prune = 0.0

        # â”€â”€ Initialize own DB (single connection, event-loop thread only) â”€â”€
        self._db_path = ctx.plugin_dir / "thrall.db"
        self._db = sqlite3.connect(str(self._db_path))
        self._db.execute("PRAGMA journal_mode=WAL")
        self._init_tables()

        # Load active prompt from DB (or use default)
        self._active_prompt = self._load_active_prompt()
        self._active_prompt_hash = thrall_mod.prompt_hash(self._active_prompt)

        # Share DB connection with admin module (same thread, same connection)
        thrall_admin_mod.init(self._db, guard=self)

        self._log.info(
            f"Thrall guard initialized: backend={thrall_cfg.get('backend', 'ollama')}, "
            f"prompt_hash={self._active_prompt_hash}, "
            f"loop_threshold={self._loop_threshold}/{self._loop_threshold_sessionless}")

    # â”€â”€ DB setup â”€â”€

    def _init_tables(self):
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS thrall_classifications (
                rowid         INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id    TEXT,
                from_node     TEXT NOT NULL,
                tier          TEXT NOT NULL,
                action        TEXT NOT NULL,
                reasoning     TEXT,
                prompt_hash   TEXT,
                wall_ms       INTEGER,
                session_id    TEXT,
                created_at    REAL NOT NULL,
                ttl_expires   REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tc_node
                ON thrall_classifications(from_node);
            CREATE INDEX IF NOT EXISTS idx_tc_action
                ON thrall_classifications(action);
            CREATE INDEX IF NOT EXISTS idx_tc_ttl
                ON thrall_classifications(ttl_expires);

            CREATE TABLE IF NOT EXISTS thrall_prompts (
                name       TEXT PRIMARY KEY,
                content    TEXT NOT NULL,
                hash       TEXT NOT NULL,
                pushed_by  TEXT NOT NULL,
                pushed_at  REAL NOT NULL,
                active     INTEGER DEFAULT 1
            );
        """)

        # Insert default prompt if not exists
        default_hash = thrall_mod.prompt_hash(thrall_mod.DEFAULT_SYSTEM_PROMPT)
        self._db.execute(
            """INSERT OR IGNORE INTO thrall_prompts
               (name, content, hash, pushed_by, pushed_at, active)
               VALUES (?, ?, ?, ?, ?, 1)""",
            ("triage", thrall_mod.DEFAULT_SYSTEM_PROMPT, default_hash,
             "hardcoded", time.time()))
        self._db.commit()

    def _load_active_prompt(self) -> str:
        """Load active triage prompt from DB. Falls back to hardcoded default."""
        row = self._db.execute(
            "SELECT content FROM thrall_prompts WHERE name = 'triage' AND active = 1"
        ).fetchone()
        if row:
            return row[0]
        return thrall_mod.DEFAULT_SYSTEM_PROMPT

    # â”€â”€ Classification records â”€â”€

    def _record_classification(self, msg_id: Optional[str], from_node: str,
                               decision: Dict[str, Any],
                               session_id: Optional[str]):
        """Write classification record to thrall.db. Called from event loop thread."""
        now = time.time()
        ttl = now + self._classification_ttl_seconds
        # Use sanitized prefix for storage (not raw from_node which is also stored)
        self._db.execute(
            """INSERT INTO thrall_classifications
               (message_id, from_node, tier, action, reasoning, prompt_hash,
                wall_ms, session_id, created_at, ttl_expires)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (msg_id, from_node, decision.get("trust_tier", "unknown"),
             decision["action"], decision.get("reasoning", "")[:2000],
             decision.get("prompt_hash", ""), decision.get("wall_ms", 0),
             session_id, now, ttl))
        self._db.commit()

    # â”€â”€ Logging â”€â”€

    def _log_event(self, action: str, node_prefix: str, detail: str = ""):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        # Sanitize: strip newlines from prefix and detail to prevent log injection
        safe_prefix = node_prefix.replace("\n", "").replace("\r", "")[:16]
        safe_detail = detail.replace("\n", " ").replace("\r", "")[:500]
        line = f"{ts} [{action}] {safe_prefix} {safe_detail}\n"
        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass
        if self._config.get("debug", False):
            self._log.debug(f"Thrall: [{action}] {safe_prefix} {safe_detail}")

    # â”€â”€ Node ID validation â”€â”€

    @staticmethod
    def _safe_prefix(from_node: str) -> str:
        """Extract validated hex prefix from node ID. Returns 'invalid' for bad IDs."""
        return thrall_mod.sanitize_node_prefix(from_node)

    # â”€â”€ Circuit breakers â”€â”€

    def _check_breakers(self, from_node: str) -> Optional[dict]:
        """Check if any breaker blocks this sender. Returns breaker dict or None."""
        if not self._breaker_dir.exists():
            return None

        now = time.time()
        prefix = self._safe_prefix(from_node)

        # Check order: global > node-specific
        for name in ("global", prefix):
            path = self._breaker_dir / f"{name}.json"
            try:
                raw = path.read_text(encoding="utf-8")
            except (OSError, FileNotFoundError):
                continue
            try:
                breaker = json.loads(raw)
            except json.JSONDecodeError:
                continue

            # Check auto-expire
            expires_at = breaker.get("expires_at")
            if expires_at:
                try:
                    exp_ts = datetime.fromisoformat(expires_at).timestamp()
                    if now > exp_ts:
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            pass
                        self._log_event("BREAKER_EXPIRED", name,
                                        f"auto-expired after {breaker.get('auto_expire_seconds', '?')}s")
                        continue
                except ValueError:
                    pass

            return breaker

        return None

    def _trip_breaker(self, breaker_type: str, target: str, reason: str,
                      auto_expire_seconds: int = 3600):
        """Create a breaker file. Target must be a validated hex prefix or 'global'."""
        # Validate target to prevent path traversal
        if target != "global" and not thrall_mod._HEX_RE.match(target):
            self._log.warning(f"Thrall: refusing breaker for invalid target: {target!r}")
            return

        self._breaker_dir.mkdir(exist_ok=True)

        now = datetime.now(timezone.utc)
        expires_at = ((now + timedelta(seconds=auto_expire_seconds)).isoformat()
                      if auto_expire_seconds > 0 else None)
        breaker = {
            "type": breaker_type,
            "target": target,
            "reason": reason[:500],
            "tripped_at": now.isoformat(),
            "trip_count": 1,
            "last_event": reason[:500],
            "auto_expire_seconds": auto_expire_seconds,
            "expires_at": expires_at,
        }

        path = self._breaker_dir / f"{target}.json"

        # Increment trip_count if breaker already exists
        try:
            raw = path.read_text(encoding="utf-8")
            existing = json.loads(raw)
            breaker["trip_count"] = existing.get("trip_count", 0) + 1
        except (OSError, FileNotFoundError, json.JSONDecodeError):
            pass

        path.write_text(json.dumps(breaker, indent=2), encoding="utf-8")
        self._log_event("BREAKER_TRIP", target, reason[:200])

    async def _wake_agent(self, breaker_type: str, target: str, reason: str):
        """Send system mail to own node to wake the agent."""
        try:
            await self._ctx.send_mail(
                to_node=self._ctx.node_id,
                msg_type="system",
                body={
                    "type": "thrall_breaker",
                    "wake_agent": True,
                    "breaker_type": breaker_type,
                    "target": target,
                    "reason": reason[:500],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
                session_id="thrall:breaker",
                system=True,
            )
        except Exception as e:
            self._log.warning(f"Thrall: agent wake failed: {e}")
            self._log_event("WAKE_FAIL", target, str(e)[:200])

    # â”€â”€ Rate limiter â”€â”€

    def _check_rate(self, node_prefix: str) -> bool:
        now = time.time()
        window = self._rate_limit.get(node_prefix, [])
        window = [t for t in window if now - t < 3600]
        if not window:
            # Remove empty entry to prevent unbounded growth (I-1)
            self._rate_limit.pop(node_prefix, None)
        else:
            self._rate_limit[node_prefix] = window
        return len(window) < self._max_per_hour

    def _record_rate(self, node_prefix: str):
        window = self._rate_limit.get(node_prefix, [])
        window.append(time.time())
        self._rate_limit[node_prefix] = window

    # â”€â”€ Solicited tracking â”€â”€

    def record_send(self, to_node: str, session_id: str):
        """Record that we sent a message (for solicited reply detection).

        MUST be called by the responder plugin when it sends a reply.
        Without this, _is_solicited always returns False and all replies
        use the base threshold (no solicited double-threshold).
        """
        key = (self._safe_prefix(to_node), session_id)
        self._solicited_sends[key] = time.time()
        # LRU eviction
        while len(self._solicited_sends) > _MAX_COUNTER_ENTRIES:
            self._solicited_sends.popitem(last=False)

    def _is_solicited(self, from_node: str, session_id: str) -> bool:
        """Check if we sent a message to this node+session (meaning their reply is solicited)."""
        key = (self._safe_prefix(from_node), session_id)
        ts = self._solicited_sends.get(key)
        if ts is None:
            return False
        if time.time() - ts > 3600:
            return False
        return True

    # â”€â”€ Loop detection â”€â”€

    def _check_loop(self, from_node: str, session_id: Optional[str]) -> Optional[str]:
        """Check for reply loops. Returns None if OK, or reason string if loop detected.

        NOTE: All mutations to _reply_counter happen synchronously within the
        asyncio event loop (no awaits between read and write). If a future refactor
        adds an await here, a lock will be needed.
        """
        prefix = self._safe_prefix(from_node)

        # Use session_id if present and not auto-generated, else node-only bucket
        if session_id and not session_id.startswith("resp:"):
            key = (session_id, prefix)
            threshold = self._loop_threshold
        else:
            key = ("default", prefix)
            threshold = self._loop_threshold_sessionless

        now = time.time()
        window = self._reply_counter.get(key, [])
        window = [t for t in window if now - t < _REPLY_WINDOW_SECONDS]
        window.append(now)

        # LRU: move to end (most recently used)
        if key in self._reply_counter:
            self._reply_counter.move_to_end(key)
        self._reply_counter[key] = window

        # LRU eviction
        while len(self._reply_counter) > _MAX_COUNTER_ENTRIES:
            self._reply_counter.popitem(last=False)

        # Solicited replies get double threshold
        solicited = self._is_solicited(from_node, session_id or "")
        effective_threshold = threshold * 2 if solicited else threshold

        if len(window) > effective_threshold:
            return (f"loop detected: {len(window)} replies from {prefix} "
                    f"in session '{session_id or 'default'}' "
                    f"(threshold: {effective_threshold}, solicited: {solicited})")

        return None

    def _check_knock_pattern(self, from_node: str) -> bool:
        """Check if a node is persistently knocking (many drops in short window).
        Returns True if knock threshold exceeded. Synchronous â€” safe on event loop
        because thrall.db is small and indexed."""
        prefix = self._safe_prefix(from_node)
        cutoff = time.time() - 3600
        # Use exact prefix match via substr, not LIKE (prevents wildcard injection)
        row = self._db.execute(
            """SELECT count(*) FROM thrall_classifications
               WHERE substr(from_node, 1, 16) = ? AND action = 'drop'
               AND created_at > ?""",
            (prefix, cutoff)).fetchone()
        count = row[0] if row else 0
        return count >= self._knock_threshold

    # â”€â”€ Main hook â”€â”€

    async def on_mail_received(self, msg_type: str, from_node: str,
                               to_node: str, body: Any,
                               session_id: Optional[str]) -> None:
        if not self._enabled:
            return

        prefix = self._safe_prefix(from_node)

        # Skip invalid node IDs
        if prefix == "invalid":
            self._log_event("SKIP_INVALID", from_node[:20], "non-hex node ID")
            return

        # Skip own node
        if from_node == self._ctx.node_id:
            return

        # Skip ignored message types
        if (msg_type or "text") in self._ignore_msg_types:
            return

        # â”€â”€ Breaker check (before any work) â”€â”€
        breaker = self._check_breakers(from_node)
        if breaker:
            self._log_event("BREAKER_BLOCKED", prefix,
                            f"breaker={breaker.get('target', '?')}: "
                            f"{breaker.get('reason', '?')}")
            self._record_classification(
                msg_id=None, from_node=from_node,
                decision={"action": "breaker_blocked",
                          "trust_tier": "unknown", "wall_ms": 0,
                          "reasoning": f"breaker: {breaker.get('reason', '?')}",
                          "prompt_hash": self._active_prompt_hash},
                session_id=session_id)
            return

        # Parse body text
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except (json.JSONDecodeError, TypeError):
                body = {"content": body}
        elif body is None:
            body = {}

        body_text = body.get("content", body.get("text", ""))
        if not body_text:
            # Truncate body BEFORE json.dumps to avoid allocating huge strings (W-8)
            preview_body = {k: (v[:_MAX_BODY_PREVIEW] if isinstance(v, str) else v)
                           for k, v in list(body.items())[:10]}
            body_text = json.dumps(preview_body)
        if not body_text.strip():
            return

        # Message ID (if available in body)
        msg_id = body.get("_handler_message_id")

        # Session
        if not session_id:
            session_id = f"resp:{prefix}"

        # â”€â”€ Triage â”€â”€
        if self._thrall_enabled:
            decision = await thrall_mod.triage(
                from_node=from_node,
                body_text=body_text,
                msg_type=msg_type or "text",
                trust_tiers=self._trust_tiers,
                config=self._thrall_config,
                log=self._log,
                active_prompt=self._active_prompt,
            )
            action = decision["action"]

            self._log_event("TRIAGE", prefix,
                            f"action={action} tier={decision['trust_tier']} "
                            f"wall={decision['wall_ms']}ms "
                            f"reason={decision.get('reason', '?')}")

            # Record every classification (including drops)
            self._record_classification(msg_id, from_node, decision, session_id)

            if action == "drop":
                # Check knock pattern (sustained drops from same node)
                if self._check_knock_pattern(from_node):
                    self._log_event("KNOCK_ALERT", prefix,
                                    f"sustained drops (threshold: {self._knock_threshold})")
                    await self._wake_agent("knock", prefix,
                                           f"sustained drops from {prefix}")
                return

            # â”€â”€ Loop detection (after triage, before forwarding) â”€â”€
            loop_reason = self._check_loop(from_node, session_id)
            if loop_reason:
                self._log_event("LOOP_DETECTED", prefix, loop_reason)
                self._trip_breaker("node", prefix, loop_reason,
                                   auto_expire_seconds=3600)
                await self._wake_agent("node", prefix, loop_reason)
                self._record_classification(
                    msg_id=None, from_node=from_node,
                    decision={"action": "loop_blocked",
                              "trust_tier": decision.get("trust_tier", "unknown"),
                              "wall_ms": 0,
                              "reasoning": loop_reason,
                              "prompt_hash": self._active_prompt_hash},
                    session_id=session_id)
                return

            # Rate limit check
            if not self._check_rate(prefix):
                self._log_event("SKIP_RATE", prefix,
                                f"rate limit ({self._max_per_hour}/hr)")
                return

        else:
            # Thrall disabled â€” no classification, no loop detection
            self._log_event("PASS_THROUGH", prefix, "thrall disabled")

        # Message passed all gates. The downstream handler (agent, responder,
        # or application code) processes it from here. Thrall's job is done.
        #
        # NOTE: This plugin does NOT call Claude or send replies. It is a
        # guard â€” it classifies, records, and blocks. The responder
        # (if enabled separately) handles auto-reply via its own plugin.

    # â”€â”€ Tick: pruning and eviction â”€â”€

    async def on_tick(self, peers: List[NodeInfo], health: NodeHealth) -> None:
        if not self._enabled:
            return

        now = time.time()
        if now - self._last_prune < _PRUNE_INTERVAL_SECONDS:
            return

        # Prune expired classification records
        deleted = self._db.execute(
            "DELETE FROM thrall_classifications WHERE ttl_expires < ?",
            (now,)).rowcount
        self._db.commit()
        if deleted:
            self._log_event("PRUNE", "-", f"removed {deleted} expired classifications")

        # Prune expired breaker files
        if self._breaker_dir.exists():
            for path in self._breaker_dir.glob("*.json"):
                try:
                    breaker = json.loads(path.read_text(encoding="utf-8"))
                    expires_at = breaker.get("expires_at")
                    if expires_at:
                        exp_ts = datetime.fromisoformat(expires_at).timestamp()
                        if now > exp_ts:
                            path.unlink(missing_ok=True)
                            self._log_event("BREAKER_EXPIRED", path.stem,
                                            "pruned on tick")
                except (json.JSONDecodeError, OSError, ValueError):
                    pass

        # Prune stale reply counter entries
        stale_keys = []
        for key, timestamps in self._reply_counter.items():
            fresh = [t for t in timestamps if now - t < _REPLY_WINDOW_SECONDS]
            if not fresh:
                stale_keys.append(key)
            else:
                self._reply_counter[key] = fresh
        for key in stale_keys:
            del self._reply_counter[key]

        # Prune stale solicited sends (older than 1 hour)
        stale_sends = [k for k, ts in self._solicited_sends.items()
                       if now - ts > 3600]
        for k in stale_sends:
            del self._solicited_sends[k]

        # Prune stale rate limit entries (empty windows)
        stale_rates = [k for k, v in self._rate_limit.items() if not v]
        for k in stale_rates:
            del self._rate_limit[k]

        self._last_prune = now

    # â”€â”€ Reload prompt on demand â”€â”€

    def reload_prompt(self):
        """Reload active prompt from DB. Called from event loop thread
        (via thrall_admin skill handler which runs on event loop)."""
        self._active_prompt = self._load_active_prompt()
        self._active_prompt_hash = thrall_mod.prompt_hash(self._active_prompt)
        self._log.info(f"Thrall: prompt reloaded, hash={self._active_prompt_hash}")

    # â”€â”€ Shutdown â”€â”€

    async def on_shutdown(self) -> None:
        if self._enabled:
            self._db.close()
            self._log.info("Thrall guard shut down")

```

### File 3: thrall_admin.py

```python
"""thrall-prompt-load â€” Operator-only skill for managing thrall prompts.

Push new classification prompts to thrall's local DB. Only the operator
node (whitelisted in knarr.toml) can call this skill.

Uses module-level singleton. Receives the DB connection from the guard
plugin (same connection, same thread â€” no write-write contention).
"""

import hashlib
import json
import sqlite3
import time
from typing import Optional


class ThrallAdmin:
    """Singleton admin interface for thrall prompt management."""

    def __init__(self):
        self._db: Optional[sqlite3.Connection] = None
        self._guard = None  # reference to ThrallGuard instance for reload

    def init(self, db: sqlite3.Connection, guard=None):
        """Initialize with shared DB connection. Called once by ThrallGuard.__init__."""
        self._db = db
        if guard:
            self._guard = guard

    async def handle(self, input_data: dict) -> dict:
        """Handle prompt-load skill call.

        Input:
            action: "load" | "list" | "get"
            name: prompt name (default: "triage")
            content: prompt text (for "load")
            from_node: caller node ID (injected by knarr)

        Output:
            status: "ok" | "error"
            + action-specific fields
        """
        if self._db is None:
            return {"status": "error", "error": "thrall admin not initialized"}

        action = input_data.get("action", "load")
        from_node = input_data.get("from_node", "unknown")

        if action == "load":
            return self._load_prompt(input_data, from_node)
        elif action == "list":
            return self._list_prompts()
        elif action == "get":
            return self._get_prompt(input_data)
        else:
            return {"status": "error", "error": f"unknown action: {action}"}

    def _load_prompt(self, input_data: dict, from_node: str) -> dict:
        name = input_data.get("name", "triage")
        content = input_data.get("content", "")

        if not content.strip():
            return {"status": "error", "error": "content required"}

        # Validate: prompt must contain {tier} placeholder
        if "{tier}" not in content:
            return {"status": "error",
                    "error": "prompt must contain {tier} placeholder"}

        p_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

        self._db.execute(
            """INSERT OR REPLACE INTO thrall_prompts
               (name, content, hash, pushed_by, pushed_at, active)
               VALUES (?, ?, ?, ?, ?, 1)""",
            (name, content, p_hash, from_node[:16], time.time()))
        self._db.commit()

        # Notify guard to reload prompt (same thread, synchronous)
        if self._guard is not None:
            self._guard.reload_prompt()

        return {"status": "ok", "prompt": name, "hash": p_hash}

    def _list_prompts(self) -> dict:
        rows = self._db.execute(
            "SELECT name, hash, pushed_by, pushed_at, active FROM thrall_prompts"
        ).fetchall()
        prompts = []
        for r in rows:
            prompts.append({
                "name": r[0], "hash": r[1], "pushed_by": r[2],
                "pushed_at": r[3], "active": bool(r[4]),
            })
        return {"status": "ok", "prompts": json.dumps(prompts)}

    def _get_prompt(self, input_data: dict) -> dict:
        name = input_data.get("name", "triage")
        row = self._db.execute(
            "SELECT content, hash, pushed_by, pushed_at FROM thrall_prompts WHERE name = ?",
            (name,)).fetchone()
        if not row:
            return {"status": "error", "error": f"prompt '{name}' not found"}
        return {
            "status": "ok", "name": name, "content": row[0],
            "hash": row[1], "pushed_by": row[2],
        }


# Module-level singleton
_admin = ThrallAdmin()


def init(db: sqlite3.Connection, guard=None):
    """Initialize the admin singleton with shared DB connection."""
    _admin.init(db, guard=guard)


async def handle(input_data: dict) -> dict:
    """Skill entry point."""
    return await _admin.handle(input_data)

```
