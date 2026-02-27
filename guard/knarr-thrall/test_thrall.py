"""Unit tests for knarr-thrall guard plugin.

Run: py -3.14 -m pytest guard/knarr-thrall/test_thrall.py -v
"""
# pytest-asyncio auto mode: all async test functions get event loops
pytest_plugins = ('pytest_asyncio',)

import asyncio
import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Minimal stubs so we can import thrall modules without knarr installed ──

import sys
import types
import dataclasses


# Stub knarr.dht.plugins
@dataclasses.dataclass
class NodeHealth:
    event_loop_lag_ms: float = 0.0
    active_connections: int = 0
    max_connections: int = 100
    write_queue_depth: int = 0
    peer_count: int = 0
    uptime_seconds: float = 0.0


@dataclasses.dataclass
class PluginContext:
    node_id: str = ""
    plugin_dir: Path = Path(".")
    get_peers: Any = None
    send_to_peer: Any = None
    send_fire_forget: Any = None
    delivery_cb: Any = None
    log: Any = None
    send_mail: Any = None


class PluginHooks:
    async def on_mail_received(self, msg_type, from_node, to_node, body, session_id):
        pass

    async def on_tick(self, peers, health):
        pass

    async def on_shutdown(self):
        pass


@dataclasses.dataclass(frozen=True)
class NodeInfo:
    node_id: str = ""
    host: str = ""
    port: int = 0


# Wire stubs into sys.modules before importing handler
_plugins_mod = types.ModuleType("knarr.dht.plugins")
_plugins_mod.PluginHooks = PluginHooks
_plugins_mod.PluginContext = PluginContext
_plugins_mod.NodeHealth = NodeHealth

_models_mod = types.ModuleType("knarr.core.models")
_models_mod.NodeInfo = NodeInfo

_dht_mod = types.ModuleType("knarr.dht")
_core_mod = types.ModuleType("knarr.core")
_knarr_mod = types.ModuleType("knarr")

sys.modules["knarr"] = _knarr_mod
sys.modules["knarr.dht"] = _dht_mod
sys.modules["knarr.dht.plugins"] = _plugins_mod
sys.modules["knarr.core"] = _core_mod
sys.modules["knarr.core.models"] = _models_mod

# Now import thrall modules (they live in the same directory)
_test_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _test_dir)

import importlib.util

_spec = importlib.util.spec_from_file_location("thrall", os.path.join(_test_dir, "thrall.py"))
thrall = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(thrall)

_hspec = importlib.util.spec_from_file_location("handler", os.path.join(_test_dir, "handler.py"))
handler = importlib.util.module_from_spec(_hspec)
_hspec.loader.exec_module(handler)


# ── Fixtures ──

@pytest.fixture
def tmp_plugin_dir(tmp_path):
    """Create a temporary plugin directory."""
    return tmp_path


@pytest.fixture
def mock_ctx(tmp_plugin_dir):
    """Create a mock PluginContext."""
    ctx = PluginContext(
        node_id="aa" * 32,
        plugin_dir=tmp_plugin_dir,
        get_peers=lambda: [],
        send_to_peer=AsyncMock(),
        send_fire_forget=AsyncMock(),
        delivery_cb=None,
        log=logging.getLogger("test_thrall"),
        send_mail=AsyncMock(),
    )
    return ctx


@pytest.fixture
def thrall_config():
    """Standard thrall guard config."""
    return {
        "enabled": True,
        "thrall": {
            "enabled": True,
            "backend": "ollama",
            "ollama_url": "http://localhost:11434",
            "timeout_seconds": 5,
            "fallback": "tier",
            "trust_tiers": {
                "team": ["bb" * 8],
                "known": ["cc" * 8],
            },
            "loop_threshold": 2,
            "loop_threshold_sessionless": 5,
            "knock_threshold": 10,
            "classification_ttl_days": 30,
        },
    }


@pytest.fixture
def guard(mock_ctx, thrall_config):
    """Create a ThrallGuard instance with mock LLM backend."""
    g = handler.ThrallGuard(mock_ctx, thrall_config)
    return g


# Helper: valid node ID (64 hex chars)
TEAM_NODE = "bb" * 32
KNOWN_NODE = "cc" * 32
UNKNOWN_NODE = "dd" * 32
OWN_NODE = "aa" * 32


# ══════════════════════════════════════════════════════════════════════
# thrall.py unit tests
# ══════════════════════════════════════════════════════════════════════

class TestPromptHash:
    def test_deterministic(self):
        h1 = thrall.prompt_hash("hello")
        h2 = thrall.prompt_hash("hello")
        assert h1 == h2

    def test_length_16(self):
        h = thrall.prompt_hash("test prompt")
        assert len(h) == 16

    def test_hex_chars(self):
        h = thrall.prompt_hash("test")
        assert all(c in "0123456789abcdef" for c in h)

    def test_different_inputs(self):
        h1 = thrall.prompt_hash("hello")
        h2 = thrall.prompt_hash("world")
        assert h1 != h2


class TestSanitizeNodePrefix:
    def test_valid_hex(self):
        assert thrall.sanitize_node_prefix("abcdef0123456789" * 4) == "abcdef0123456789"

    def test_uppercase_lowered(self):
        assert thrall.sanitize_node_prefix("ABCDEF0123456789" * 4) == "abcdef0123456789"

    def test_invalid_chars(self):
        assert thrall.sanitize_node_prefix("not-hex-at-all!!") == "invalid"

    def test_short_valid(self):
        # Shorter than 16 chars but valid hex — regex allows it (prefix matching)
        assert thrall.sanitize_node_prefix("abcd") == "abcd"

    def test_empty(self):
        assert thrall.sanitize_node_prefix("") == "invalid"

    def test_newline_injection(self):
        assert thrall.sanitize_node_prefix("abcd\nef0123456789") == "invalid"

    def test_path_traversal(self):
        assert thrall.sanitize_node_prefix("../../etc/passwd") == "invalid"

    def test_sql_wildcards(self):
        assert thrall.sanitize_node_prefix("abcdef01234567%_") == "invalid"


class TestResolveTier:
    def test_team(self):
        tiers = {"team": ["bbbb"], "known": ["cccc"]}
        assert thrall._resolve_tier("bbbb" + "0" * 60, tiers) == "team"

    def test_known(self):
        tiers = {"team": ["bbbb"], "known": ["cccc"]}
        assert thrall._resolve_tier("cccc" + "0" * 60, tiers) == "known"

    def test_unknown(self):
        tiers = {"team": ["bbbb"], "known": ["cccc"]}
        assert thrall._resolve_tier("dddd" + "0" * 60, tiers) == "unknown"

    def test_empty_tiers(self):
        assert thrall._resolve_tier("anything", {}) == "unknown"


class TestTierFallback:
    def test_team_fallback(self):
        assert thrall._tier_fallback_action("team", {}) == "wake"

    def test_known_fallback(self):
        assert thrall._tier_fallback_action("known", {}) == "wake"

    def test_unknown_fallback(self):
        assert thrall._tier_fallback_action("unknown", {}) == "drop"

    def test_forced_wake(self):
        assert thrall._tier_fallback_action("unknown", {"fallback": "wake"}) == "wake"

    def test_forced_drop(self):
        assert thrall._tier_fallback_action("team", {"fallback": "drop"}) == "drop"


class TestEmbeddedBackendInferenceLock:
    """Verify that classify() serializes through _infer_lock (GPT C-1 fix)."""

    def test_infer_lock_exists(self):
        assert hasattr(thrall.EmbeddedBackend, "_infer_lock")
        assert isinstance(thrall.EmbeddedBackend._infer_lock, type(threading.Lock()))

    def test_load_failed_flag(self):
        """Q-2: After failed model load, subsequent calls skip retry."""
        backend = thrall.EmbeddedBackend({"model_path": "/nonexistent/model.gguf"})
        # Reset state for test isolation
        original_instance = thrall.EmbeddedBackend._instance
        original_failed = thrall.EmbeddedBackend._load_failed
        thrall.EmbeddedBackend._instance = None
        thrall.EmbeddedBackend._load_failed = False
        try:
            with pytest.raises(Exception):
                backend._ensure_loaded()
            assert thrall.EmbeddedBackend._load_failed is True
            # Second call should fail fast without retrying
            with pytest.raises(RuntimeError, match="previously failed"):
                backend._ensure_loaded()
        finally:
            thrall.EmbeddedBackend._instance = original_instance
            thrall.EmbeddedBackend._load_failed = original_failed


@pytest.mark.asyncio
class TestTriage:
    async def test_team_bypass(self):
        """Team nodes skip LLM call entirely."""
        result = await thrall.triage(
            from_node=TEAM_NODE,
            body_text="hello",
            msg_type="text",
            trust_tiers={"team": ["bb" * 8]},
            config={"backend": "ollama"},
            log=logging.getLogger("test"),
        )
        assert result["action"] == "wake"
        assert result["trust_tier"] == "team"
        assert "bypass" in result["reason"].lower()

    async def test_backend_error_fallback(self):
        """Backend failure falls back to tier-based action."""
        with patch.object(thrall, "_get_backend") as mock_backend:
            mock_backend.return_value.classify.side_effect = RuntimeError("timeout")
            result = await thrall.triage(
                from_node=UNKNOWN_NODE,
                body_text="hello",
                msg_type="text",
                trust_tiers={},
                config={"backend": "ollama", "fallback": "tier"},
                log=logging.getLogger("test"),
            )
            assert result["action"] == "drop"  # unknown tier → drop
            assert "error" in result["reason"].lower()

    async def test_bad_action_fallback(self):
        """Unrecognized LLM action falls back to tier-based action."""
        with patch.object(thrall, "_get_backend") as mock_backend:
            mock_backend.return_value.classify.return_value = {"action": "INVALID", "reason": "test"}
            result = await thrall.triage(
                from_node=KNOWN_NODE,
                body_text="hello",
                msg_type="text",
                trust_tiers={"known": ["cc" * 8]},
                config={"backend": "ollama", "fallback": "tier"},
                log=logging.getLogger("test"),
            )
            assert result["action"] == "wake"  # known tier → wake fallback
            assert "bad" in result["reason"].lower() or "fallback" in result["reason"].lower()

    async def test_successful_classification(self):
        """Normal LLM classification returns the model's decision."""
        with patch.object(thrall, "_get_backend") as mock_backend:
            mock_backend.return_value.classify.return_value = {"action": "wake", "reason": "skill request"}
            result = await thrall.triage(
                from_node=UNKNOWN_NODE,
                body_text="Can you run digest-voice?",
                msg_type="text",
                trust_tiers={},
                config={"backend": "ollama"},
                log=logging.getLogger("test"),
            )
            assert result["action"] == "wake"
            assert result["trust_tier"] == "unknown"
            assert result["prompt_hash"]  # non-empty

    async def test_body_truncated_to_800(self):
        """Body text passed to backend is truncated to 800 chars."""
        with patch.object(thrall, "_get_backend") as mock_backend:
            mock_backend.return_value.classify.return_value = {"action": "drop", "reason": "spam"}
            long_body = "x" * 2000
            await thrall.triage(
                from_node=UNKNOWN_NODE,
                body_text=long_body,
                msg_type="text",
                trust_tiers={},
                config={"backend": "ollama"},
                log=logging.getLogger("test"),
            )
            call_args = mock_backend.return_value.classify.call_args
            assert len(call_args[0][1]) == 800  # second positional arg is body_text


# ══════════════════════════════════════════════════════════════════════
# handler.py unit tests
# ══════════════════════════════════════════════════════════════════════

class TestGuardInit:
    def test_db_created(self, guard, tmp_plugin_dir):
        """thrall.db is created in plugin_dir."""
        assert (tmp_plugin_dir / "thrall.db").exists()

    def test_default_prompt_loaded(self, guard):
        assert guard._active_prompt is not None
        assert len(guard._active_prompt_hash) == 16

    def test_tables_exist(self, guard):
        tables = guard._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {r[0] for r in tables}
        assert "thrall_classifications" in names
        assert "thrall_prompts" in names

    def test_disabled_guard(self, mock_ctx):
        g = handler.ThrallGuard(mock_ctx, {"enabled": False})
        assert not g._enabled


class TestBodyParsing:
    """GPT C-2: body can be any JSON type, not just dict."""

    @pytest.mark.asyncio
    async def test_dict_body(self, guard):
        """Normal dict body with content field."""
        with patch.object(handler.thrall_mod, "triage", new_callable=AsyncMock) as mock_triage:
            mock_triage.return_value = {
                "action": "wake", "reason": "test", "trust_tier": "unknown",
                "wall_ms": 10, "reasoning": "{}", "prompt_hash": "abc",
            }
            await guard.on_mail_received(
                "text", UNKNOWN_NODE, OWN_NODE,
                {"content": "hello world"}, None)
            mock_triage.assert_called_once()
            assert mock_triage.call_args.kwargs["body_text"] == "hello world"

    @pytest.mark.asyncio
    async def test_string_body(self, guard):
        """Plain string body."""
        with patch.object(handler.thrall_mod, "triage", new_callable=AsyncMock) as mock_triage:
            mock_triage.return_value = {
                "action": "wake", "reason": "test", "trust_tier": "unknown",
                "wall_ms": 10, "reasoning": "{}", "prompt_hash": "abc",
            }
            await guard.on_mail_received(
                "text", UNKNOWN_NODE, OWN_NODE,
                "hello world", None)
            mock_triage.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_body_no_crash(self, guard):
        """List body should not crash (GPT C-2)."""
        with patch.object(handler.thrall_mod, "triage", new_callable=AsyncMock) as mock_triage:
            mock_triage.return_value = {
                "action": "drop", "reason": "test", "trust_tier": "unknown",
                "wall_ms": 10, "reasoning": "{}", "prompt_hash": "abc",
            }
            # Should not raise AttributeError
            await guard.on_mail_received(
                "text", UNKNOWN_NODE, OWN_NODE,
                ["item1", "item2"], None)

    @pytest.mark.asyncio
    async def test_number_body_no_crash(self, guard):
        """Number body should not crash (GPT C-2)."""
        with patch.object(handler.thrall_mod, "triage", new_callable=AsyncMock) as mock_triage:
            mock_triage.return_value = {
                "action": "drop", "reason": "test", "trust_tier": "unknown",
                "wall_ms": 10, "reasoning": "{}", "prompt_hash": "abc",
            }
            await guard.on_mail_received(
                "text", UNKNOWN_NODE, OWN_NODE,
                42, None)

    @pytest.mark.asyncio
    async def test_none_body(self, guard):
        """None body → becomes {} → json.dumps is '{}' → passes to triage as fallback."""
        with patch.object(handler.thrall_mod, "triage", new_callable=AsyncMock) as mock_triage:
            mock_triage.return_value = {
                "action": "drop", "reason": "empty", "trust_tier": "unknown",
                "wall_ms": 0, "reasoning": "{}", "prompt_hash": "abc",
            }
            await guard.on_mail_received(
                "text", UNKNOWN_NODE, OWN_NODE,
                None, None)
            # None → {} → json.dumps("{}") is not empty, so triage is called
            mock_triage.assert_called_once()

    @pytest.mark.asyncio
    async def test_json_string_parsed(self, guard):
        """JSON string containing a dict gets parsed."""
        with patch.object(handler.thrall_mod, "triage", new_callable=AsyncMock) as mock_triage:
            mock_triage.return_value = {
                "action": "wake", "reason": "test", "trust_tier": "unknown",
                "wall_ms": 10, "reasoning": "{}", "prompt_hash": "abc",
            }
            await guard.on_mail_received(
                "text", UNKNOWN_NODE, OWN_NODE,
                json.dumps({"content": "parsed body"}), None)
            mock_triage.assert_called_once()
            assert mock_triage.call_args.kwargs["body_text"] == "parsed body"


class TestSkipRules:
    @pytest.mark.asyncio
    async def test_skip_own_node(self, guard):
        """Messages from own node are skipped."""
        with patch.object(handler.thrall_mod, "triage", new_callable=AsyncMock) as mock_triage:
            await guard.on_mail_received("text", OWN_NODE, OWN_NODE, {"content": "hi"}, None)
            mock_triage.assert_not_called()

    @pytest.mark.asyncio
    async def test_skip_ack(self, guard):
        """Ack messages are skipped."""
        with patch.object(handler.thrall_mod, "triage", new_callable=AsyncMock) as mock_triage:
            await guard.on_mail_received("ack", UNKNOWN_NODE, OWN_NODE, {"content": "hi"}, None)
            mock_triage.assert_not_called()

    @pytest.mark.asyncio
    async def test_skip_system(self, guard):
        with patch.object(handler.thrall_mod, "triage", new_callable=AsyncMock) as mock_triage:
            await guard.on_mail_received("system", UNKNOWN_NODE, OWN_NODE, {"content": "hi"}, None)
            mock_triage.assert_not_called()

    @pytest.mark.asyncio
    async def test_skip_invalid_node_id(self, guard):
        """Non-hex node IDs are skipped."""
        with patch.object(handler.thrall_mod, "triage", new_callable=AsyncMock) as mock_triage:
            await guard.on_mail_received("text", "not-hex!!", OWN_NODE, {"content": "hi"}, None)
            mock_triage.assert_not_called()


class TestClassificationRecording:
    @pytest.mark.asyncio
    async def test_classification_recorded(self, guard):
        """Every triage decision is recorded in thrall_classifications."""
        with patch.object(handler.thrall_mod, "triage", new_callable=AsyncMock) as mock_triage:
            mock_triage.return_value = {
                "action": "drop", "reason": "spam", "trust_tier": "unknown",
                "wall_ms": 50, "reasoning": '{"action":"drop"}', "prompt_hash": "abc123",
            }
            await guard.on_mail_received("text", UNKNOWN_NODE, OWN_NODE, {"content": "buy now"}, None)
            # Force flush
            guard._flush_commits()

            rows = guard._db.execute("SELECT * FROM thrall_classifications").fetchall()
            assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_batched_commits(self, guard):
        """Commits are batched, not per-message (Gemini C-3)."""
        guard._commit_threshold = 5
        with patch.object(handler.thrall_mod, "triage", new_callable=AsyncMock) as mock_triage:
            mock_triage.return_value = {
                "action": "drop", "reason": "spam", "trust_tier": "unknown",
                "wall_ms": 10, "reasoning": "{}", "prompt_hash": "abc",
            }
            # Send 3 messages — under threshold, no auto-commit
            for i in range(3):
                node = f"dd{i:02d}" + "00" * 30
                await guard.on_mail_received("text", node, OWN_NODE, {"content": f"msg {i}"}, None)

            assert guard._pending_commits == 3

            # Flush on tick
            await guard.on_tick([], NodeHealth())
            assert guard._pending_commits == 0


class TestBreakerCache:
    """Gemini C-2: breaker cache avoids disk I/O per message."""

    def test_cache_populated_on_check(self, guard, tmp_plugin_dir):
        """First check populates cache from disk."""
        breaker_dir = tmp_plugin_dir / "breakers"
        breaker_dir.mkdir()
        (breaker_dir / "global.json").write_text(json.dumps({
            "type": "global", "target": "global", "reason": "test",
            "tripped_at": "2026-01-01T00:00:00", "trip_count": 1,
            "auto_expire_seconds": 0,
        }))

        result = guard._check_breakers(UNKNOWN_NODE)
        assert result is not None
        assert "global" in guard._breaker_cache

    def test_cache_hit(self, guard, tmp_plugin_dir):
        """Second check returns from cache without disk read."""
        breaker_dir = tmp_plugin_dir / "breakers"
        breaker_dir.mkdir()
        (breaker_dir / "global.json").write_text(json.dumps({
            "type": "global", "target": "global", "reason": "cached test",
            "tripped_at": "2026-01-01T00:00:00", "trip_count": 1,
            "auto_expire_seconds": 0,
        }))

        # First read: from disk
        guard._check_breakers(UNKNOWN_NODE)
        # Delete file — cache should still work
        (breaker_dir / "global.json").unlink()
        result = guard._check_breakers(UNKNOWN_NODE)
        assert result is not None
        assert result["reason"] == "cached test"

    def test_cache_invalidated_on_trip(self, guard, tmp_plugin_dir):
        """Tripping a breaker invalidates its cache entry."""
        prefix = guard._safe_prefix(UNKNOWN_NODE)
        guard._breaker_cache[prefix] = (time.time(), None)

        guard._trip_breaker("node", prefix, "test trip")
        assert prefix not in guard._breaker_cache


class TestShutdownGuard:
    """Gemini C-1: shutdown race prevention."""

    @pytest.mark.asyncio
    async def test_shutdown_prevents_writes(self, guard):
        """After shutdown flag, _record_classification is a no-op."""
        guard._shutting_down = True
        guard._record_classification(
            None, UNKNOWN_NODE,
            {"action": "drop", "trust_tier": "unknown", "wall_ms": 0,
             "reasoning": "test", "prompt_hash": "abc"},
            None)
        # Nothing written
        rows = guard._db.execute("SELECT * FROM thrall_classifications").fetchall()
        assert len(rows) == 0

    @pytest.mark.asyncio
    async def test_shutdown_skips_triage(self, guard):
        """Shutdown flag prevents new triage calls."""
        guard._shutting_down = True
        with patch.object(handler.thrall_mod, "triage", new_callable=AsyncMock) as mock_triage:
            await guard.on_mail_received("text", UNKNOWN_NODE, OWN_NODE, {"content": "hi"}, None)
            mock_triage.assert_not_called()

    @pytest.mark.asyncio
    async def test_shutdown_flushes_pending(self, guard):
        """on_shutdown flushes pending commits before closing DB."""
        guard._record_classification(
            None, UNKNOWN_NODE,
            {"action": "drop", "trust_tier": "unknown", "wall_ms": 0,
             "reasoning": "test", "prompt_hash": "abc"},
            None)
        assert guard._pending_commits > 0

        await guard.on_shutdown()
        # DB is closed, but data should have been flushed
        # Reopen to verify
        db = sqlite3.connect(str(guard._db_path))
        rows = db.execute("SELECT * FROM thrall_classifications").fetchall()
        db.close()
        assert len(rows) == 1


class TestLoopDetection:
    def test_no_loop_under_threshold(self, guard):
        """Messages under threshold don't trigger loop."""
        # threshold=2 → triggers on 3rd
        assert guard._check_loop(UNKNOWN_NODE, "sess1") is None
        assert guard._check_loop(UNKNOWN_NODE, "sess1") is None

    def test_loop_on_threshold_exceeded(self, guard):
        """3rd message in session triggers loop (threshold=2)."""
        guard._check_loop(UNKNOWN_NODE, "sess1")
        guard._check_loop(UNKNOWN_NODE, "sess1")
        result = guard._check_loop(UNKNOWN_NODE, "sess1")
        assert result is not None
        assert "loop detected" in result

    def test_sessionless_higher_threshold(self, guard):
        """Sessionless messages use higher threshold (default 5)."""
        for _ in range(5):
            assert guard._check_loop(UNKNOWN_NODE, None) is None
        result = guard._check_loop(UNKNOWN_NODE, None)
        assert result is not None

    def test_different_sessions_independent(self, guard):
        """Different sessions have independent counters."""
        guard._check_loop(UNKNOWN_NODE, "sess1")
        guard._check_loop(UNKNOWN_NODE, "sess1")
        # sess1 at 2/2 threshold
        # sess2 fresh
        assert guard._check_loop(UNKNOWN_NODE, "sess2") is None

    def test_solicited_doubles_threshold(self, guard):
        """Solicited replies get double threshold."""
        # Record a send to this node+session
        guard.record_send(UNKNOWN_NODE, "sess1")

        # Now threshold is 2*2=4
        for _ in range(4):
            assert guard._check_loop(UNKNOWN_NODE, "sess1") is None
        # 5th triggers
        result = guard._check_loop(UNKNOWN_NODE, "sess1")
        assert result is not None
        assert "solicited: True" in result

    def test_lru_eviction(self, guard):
        """Counter entries are evicted when cap is reached."""
        # Force low cap
        original = handler._MAX_COUNTER_ENTRIES
        handler._MAX_COUNTER_ENTRIES = 5
        try:
            for i in range(10):
                node = f"{i:032x}" + "0" * 32
                guard._check_loop(node, f"sess_{i}")
            assert len(guard._reply_counter) <= 5
        finally:
            handler._MAX_COUNTER_ENTRIES = original


class TestRateLimiter:
    def test_under_limit_passes(self, guard):
        assert guard._check_rate("dddddddddddddddd") is True

    def test_over_limit_blocks(self, guard):
        prefix = "dddddddddddddddd"
        for _ in range(guard._max_per_hour):
            guard._record_rate(prefix)
        assert guard._check_rate(prefix) is False

    @pytest.mark.asyncio
    async def test_record_rate_called_after_pass(self, guard):
        """GPT W-1: _record_rate is called when message passes triage."""
        with patch.object(handler.thrall_mod, "triage", new_callable=AsyncMock) as mock_triage:
            mock_triage.return_value = {
                "action": "wake", "reason": "test", "trust_tier": "unknown",
                "wall_ms": 10, "reasoning": "{}", "prompt_hash": "abc",
            }
            await guard.on_mail_received("text", UNKNOWN_NODE, OWN_NODE, {"content": "hi"}, None)
            prefix = guard._safe_prefix(UNKNOWN_NODE)
            assert prefix in guard._rate_limit
            assert len(guard._rate_limit[prefix]) == 1


class TestBreakerTripping:
    @pytest.mark.asyncio
    async def test_loop_trips_breaker(self, guard):
        """Loop detection trips a node breaker."""
        with patch.object(handler.thrall_mod, "triage", new_callable=AsyncMock) as mock_triage:
            mock_triage.return_value = {
                "action": "wake", "reason": "test", "trust_tier": "unknown",
                "wall_ms": 10, "reasoning": "{}", "prompt_hash": "abc",
            }
            # Send enough to trigger loop (threshold=2, triggers on 3rd)
            for _ in range(3):
                await guard.on_mail_received("text", UNKNOWN_NODE, OWN_NODE, {"content": "hi"}, "loop_sess")

            prefix = guard._safe_prefix(UNKNOWN_NODE)
            breaker_path = guard._breaker_dir / f"{prefix}.json"
            assert breaker_path.exists()

    @pytest.mark.asyncio
    async def test_loop_wakes_agent(self, guard, mock_ctx):
        """Q-4: Loop detection calls send_mail to wake agent."""
        with patch.object(handler.thrall_mod, "triage", new_callable=AsyncMock) as mock_triage:
            mock_triage.return_value = {
                "action": "wake", "reason": "test", "trust_tier": "unknown",
                "wall_ms": 10, "reasoning": "{}", "prompt_hash": "abc",
            }
            for _ in range(3):
                await guard.on_mail_received("text", UNKNOWN_NODE, OWN_NODE, {"content": "hi"}, "wake_sess")

            assert mock_ctx.send_mail.called
            wake_call = mock_ctx.send_mail.call_args
            assert wake_call.kwargs["body"]["type"] == "thrall_breaker"
            assert wake_call.kwargs["body"]["wake_agent"] is True
            assert wake_call.kwargs["msg_type"] == "system"

    def test_breaker_blocks_message(self, guard, tmp_plugin_dir):
        """Active breaker blocks messages."""
        prefix = guard._safe_prefix(UNKNOWN_NODE)
        guard._trip_breaker("node", prefix, "test block")
        result = guard._check_breakers(UNKNOWN_NODE)
        assert result is not None

    def test_invalid_target_rejected(self, guard):
        """Path-traversal target is rejected."""
        guard._trip_breaker("node", "../../etc", "evil")
        assert not (guard._breaker_dir / "../../etc.json").exists()

    def test_breaker_auto_expire(self, guard, tmp_plugin_dir):
        """Expired breaker returns None and deletes file."""
        breaker_dir = tmp_plugin_dir / "breakers"
        breaker_dir.mkdir()
        expired = {
            "type": "node", "target": "dddddddddddddddd", "reason": "test",
            "tripped_at": "2020-01-01T00:00:00", "trip_count": 1,
            "auto_expire_seconds": 1,
            "expires_at": "2020-01-01T00:00:01+00:00",
        }
        (breaker_dir / "dddddddddddddddd.json").write_text(json.dumps(expired))

        result = guard._check_breakers(UNKNOWN_NODE)
        assert result is None


class TestPromptReload:
    def test_reload_from_db(self, guard):
        """reload_prompt picks up DB changes."""
        new_prompt = "New prompt with {tier} placeholder"
        guard._db.execute(
            "INSERT OR REPLACE INTO thrall_prompts (name, content, hash, pushed_by, pushed_at, active) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            ("triage", new_prompt, "newhash", "test", time.time()))
        guard._db.commit()

        guard.reload_prompt()
        assert guard._active_prompt == new_prompt


class TestPruning:
    @pytest.mark.asyncio
    async def test_expired_records_pruned(self, guard):
        """on_tick prunes expired classification records."""
        # Insert an expired record
        guard._db.execute(
            "INSERT INTO thrall_classifications "
            "(message_id, from_node, tier, action, reasoning, prompt_hash, wall_ms, session_id, created_at, ttl_expires) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (None, UNKNOWN_NODE, "unknown", "drop", "", "", 0, None, time.time() - 100, time.time() - 50))
        guard._db.commit()

        # Force prune
        guard._last_prune = 0
        await guard.on_tick([], NodeHealth())

        rows = guard._db.execute("SELECT * FROM thrall_classifications").fetchall()
        assert len(rows) == 0

    @pytest.mark.asyncio
    async def test_stale_rate_entries_pruned(self, guard):
        """on_tick removes empty rate limit entries."""
        guard._rate_limit["dead_node"] = []
        guard._last_prune = 0
        await guard.on_tick([], NodeHealth())
        assert "dead_node" not in guard._rate_limit


class TestKnockPattern:
    def test_under_threshold(self, guard):
        """Few drops don't trigger knock alert."""
        assert guard._check_knock_pattern(UNKNOWN_NODE) is False

    def test_over_threshold(self, guard):
        """Many drops trigger knock alert."""
        prefix = guard._safe_prefix(UNKNOWN_NODE)
        now = time.time()
        for i in range(15):
            guard._db.execute(
                "INSERT INTO thrall_classifications "
                "(message_id, from_node, tier, action, reasoning, prompt_hash, wall_ms, session_id, created_at, ttl_expires) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (None, UNKNOWN_NODE, "unknown", "drop", "", "", 0, None, now - i, now + 86400))
        guard._db.commit()
        assert guard._check_knock_pattern(UNKNOWN_NODE) is True


class TestAdminModule:
    """Test thrall_admin integration via the shared DB."""

    @pytest.mark.asyncio
    async def test_list_prompts(self, guard):
        """Admin list returns the default prompt."""
        import importlib.util as ilu
        admin_spec = ilu.spec_from_file_location(
            "thrall_admin", os.path.join(_test_dir, "thrall_admin.py"))
        admin = ilu.module_from_spec(admin_spec)
        admin_spec.loader.exec_module(admin)
        admin.init(guard._db, guard=guard)

        result = await admin.handle({"action": "list"})
        assert result["status"] == "ok"
        prompts = result["prompts"]  # W-4 fix: now returns list directly, not JSON string
        assert len(prompts) >= 1
        assert prompts[0]["name"] == "triage"

    @pytest.mark.asyncio
    async def test_load_prompt_validates_tier(self, guard):
        """Prompt without {tier} placeholder is rejected."""
        import importlib.util as ilu
        admin_spec = ilu.spec_from_file_location(
            "thrall_admin", os.path.join(_test_dir, "thrall_admin.py"))
        admin = ilu.module_from_spec(admin_spec)
        admin_spec.loader.exec_module(admin)
        admin.init(guard._db, guard=guard)

        result = await admin.handle({
            "action": "load",
            "content": "No tier placeholder here",
            "from_node": "test",
        })
        assert result["status"] == "error"
        assert "tier" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_load_prompt_success(self, guard):
        """Valid prompt load succeeds and triggers reload."""
        import importlib.util as ilu
        admin_spec = ilu.spec_from_file_location(
            "thrall_admin", os.path.join(_test_dir, "thrall_admin.py"))
        admin = ilu.module_from_spec(admin_spec)
        admin_spec.loader.exec_module(admin)
        admin.init(guard._db, guard=guard)

        new_prompt = "Classify messages. Trust: {tier}. Reply JSON."
        result = await admin.handle({
            "action": "load",
            "content": new_prompt,
            "from_node": "operator123",
        })
        assert result["status"] == "ok"
        assert result["hash"]
        # Guard should have reloaded
        assert guard._active_prompt == new_prompt
