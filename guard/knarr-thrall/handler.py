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

import asyncio
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
        # Populated by record_send() — must be called by the responder plugin
        # when it sends a reply, otherwise _is_solicited always returns False
        # and all replies use the base threshold.
        self._solicited_sends: OrderedDict[Tuple[str, str], float] = OrderedDict()

        # Classification TTL
        self._classification_ttl_seconds = int(
            thrall_cfg.get("classification_ttl_days", _DEFAULT_TTL_DAYS)) * 86400

        # Breaker directory + in-memory cache (C-2: avoid disk I/O per message)
        self._breaker_dir = ctx.plugin_dir / "breakers"
        self._breaker_cache: Dict[str, Tuple[float, Optional[dict]]] = {}  # name → (cached_at, breaker|None)
        self._breaker_cache_ttl = 30.0  # seconds

        # Log file
        self._log_path = ctx.plugin_dir / "thrall.log"

        # Pruning timestamp
        self._last_prune = 0.0

        # Shutdown guard: prevents DB writes after close (C-1 fix)
        self._shutting_down = False
        self._inflight = 0  # count of in-flight triage calls

        # Batched commits: accumulate INSERTs, commit on tick or threshold (C-3 fix)
        self._pending_commits = 0
        self._commit_threshold = 10  # commit every N inserts if tick hasn't fired

        # ── Initialize own DB (single connection, event-loop thread only) ──
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

    # ── DB setup ──

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

    # ── Classification records ──

    def _record_classification(self, msg_id: Optional[str], from_node: str,
                               decision: Dict[str, Any],
                               session_id: Optional[str]):
        """Write classification record to thrall.db. Called from event loop thread.
        Skips write if shutdown is in progress (C-1: shutdown race guard)."""
        if self._shutting_down:
            return
        now = time.time()
        ttl = now + self._classification_ttl_seconds
        self._db.execute(
            """INSERT INTO thrall_classifications
               (message_id, from_node, tier, action, reasoning, prompt_hash,
                wall_ms, session_id, created_at, ttl_expires)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (msg_id, from_node, decision.get("trust_tier", "unknown"),
             decision["action"], decision.get("reasoning", "")[:2000],
             decision.get("prompt_hash", ""), decision.get("wall_ms", 0),
             session_id, now, ttl))
        self._pending_commits += 1
        if self._pending_commits >= self._commit_threshold:
            self._flush_commits()

    def _flush_commits(self):
        """Commit pending DB writes. Called from tick or when threshold reached."""
        if self._pending_commits > 0 and not self._shutting_down:
            self._db.commit()
            self._pending_commits = 0

    # ── Logging ──

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

    # ── Node ID validation ──

    @staticmethod
    def _safe_prefix(from_node: str) -> str:
        """Extract validated hex prefix from node ID. Returns 'invalid' for bad IDs."""
        return thrall_mod.sanitize_node_prefix(from_node)

    # ── Circuit breakers ──

    def _load_breaker(self, name: str) -> Optional[dict]:
        """Load a single breaker from disk, checking expiry. Returns dict or None."""
        path = self._breaker_dir / f"{name}.json"
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, FileNotFoundError):
            return None
        try:
            breaker = json.loads(raw)
        except json.JSONDecodeError:
            return None

        # Check auto-expire
        expires_at = breaker.get("expires_at")
        if expires_at:
            try:
                exp_ts = datetime.fromisoformat(expires_at).timestamp()
                if time.time() > exp_ts:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    self._log_event("BREAKER_EXPIRED", name,
                                    f"auto-expired after {breaker.get('auto_expire_seconds', '?')}s")
                    return None
            except ValueError:
                pass

        return breaker

    def _get_breaker_cached(self, name: str) -> Optional[dict]:
        """Get breaker by name with in-memory cache (C-2: avoid disk I/O per message)."""
        now = time.time()
        cached = self._breaker_cache.get(name)
        if cached is not None:
            cached_at, breaker = cached
            if now - cached_at < self._breaker_cache_ttl:
                return breaker

        # Cache miss or stale — read from disk
        breaker = self._load_breaker(name)
        self._breaker_cache[name] = (now, breaker)
        return breaker

    def _check_breakers(self, from_node: str) -> Optional[dict]:
        """Check if any breaker blocks this sender. Returns breaker dict or None.
        Uses in-memory cache to avoid disk reads on every message (C-2)."""
        if not self._breaker_dir.exists():
            return None

        prefix = self._safe_prefix(from_node)

        # Check order: global > node-specific
        for name in ("global", prefix):
            breaker = self._get_breaker_cached(name)
            if breaker is not None:
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
        # Invalidate cache for this breaker (C-2)
        self._breaker_cache.pop(target, None)
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

    # ── Rate limiter ──

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

    # ── Solicited tracking ──

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

    # ── Loop detection ──

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
        Returns True if knock threshold exceeded. Synchronous — safe on event loop
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

    # ── Main hook ──

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

        # ── Breaker check (before any work) ──
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

        # Parse body text — handle any shape (str, list, int, None, dict)
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except (json.JSONDecodeError, TypeError):
                body = {"content": body}
        if body is None:
            body = {}
        if not isinstance(body, dict):
            # Non-dict JSON (list, number, bool) — wrap it (GPT C-2: remote DoS fix)
            body = {"content": str(body)}

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

        # ── Triage ──
        if self._thrall_enabled:
            if self._shutting_down:
                return

            self._inflight += 1
            try:
                decision = await thrall_mod.triage(
                    from_node=from_node,
                    body_text=body_text,
                    msg_type=msg_type or "text",
                    trust_tiers=self._trust_tiers,
                    config=self._thrall_config,
                    log=self._log,
                    active_prompt=self._active_prompt,
                )
            finally:
                self._inflight -= 1

            if self._shutting_down:
                return

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

            # ── Loop detection (after triage, before forwarding) ──
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
            self._record_rate(prefix)  # GPT W-1: was never called, rate limiter was dead

        else:
            # Thrall disabled — no classification, no loop detection
            self._log_event("PASS_THROUGH", prefix, "thrall disabled")

        # Message passed all gates. The downstream handler (agent, responder,
        # or application code) processes it from here. Thrall's job is done.
        #
        # NOTE: This plugin does NOT call Claude or send replies. It is a
        # guard — it classifies, records, and blocks. The responder
        # (if enabled separately) handles auto-reply via its own plugin.

    # ── Tick: pruning and eviction ──

    async def on_tick(self, peers: List[NodeInfo], health: NodeHealth) -> None:
        if not self._enabled:
            return

        # Flush pending DB commits on every tick (C-3: batched commits)
        self._flush_commits()

        now = time.time()
        if now - self._last_prune < _PRUNE_INTERVAL_SECONDS:
            return

        # Prune expired classification records (prune commit is standalone, not batched)
        deleted = self._db.execute(
            "DELETE FROM thrall_classifications WHERE ttl_expires < ?",
            (now,)).rowcount
        if deleted:
            self._db.commit()
            self._log_event("PRUNE", "-", f"removed {deleted} expired classifications")

        # Prune expired breaker files + invalidate cache
        if self._breaker_dir.exists():
            for path in self._breaker_dir.glob("*.json"):
                try:
                    breaker = json.loads(path.read_text(encoding="utf-8"))
                    expires_at = breaker.get("expires_at")
                    if expires_at:
                        exp_ts = datetime.fromisoformat(expires_at).timestamp()
                        if now > exp_ts:
                            path.unlink(missing_ok=True)
                            self._breaker_cache.pop(path.stem, None)
                            self._log_event("BREAKER_EXPIRED", path.stem,
                                            "pruned on tick")
                except (json.JSONDecodeError, OSError, ValueError):
                    pass
        # Clear entire breaker cache on prune tick (refresh from disk)
        self._breaker_cache.clear()

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

    # ── Reload prompt on demand ──

    def reload_prompt(self):
        """Reload active prompt from DB. Called from event loop thread
        (via thrall_admin skill handler which runs on event loop)."""
        self._active_prompt = self._load_active_prompt()
        self._active_prompt_hash = thrall_mod.prompt_hash(self._active_prompt)
        self._log.info(f"Thrall: prompt reloaded, hash={self._active_prompt_hash}")

    # ── Shutdown ──

    async def on_shutdown(self) -> None:
        if not self._enabled:
            return

        # Signal shutdown — no new triage calls or DB writes accepted
        self._shutting_down = True

        # Wait for in-flight triage calls to complete (max 15s)
        for _ in range(150):
            if self._inflight <= 0:
                break
            await asyncio.sleep(0.1)

        # Flush any remaining uncommitted writes
        if self._pending_commits > 0:
            try:
                self._db.commit()
            except sqlite3.ProgrammingError:
                pass  # DB already closed somehow

        self._db.close()
        self._log.info("Thrall guard shut down")
