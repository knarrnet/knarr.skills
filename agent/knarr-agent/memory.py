"""SQLite memory for the agent: events, conversations, state cursors, rate limits."""

import contextlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


class AgentMemory:
    def __init__(self, db_path: Path):
        self._db_path = str(db_path)
        self._init_db()

    @contextlib.contextmanager
    def _db(self):
        conn = sqlite3.connect(self._db_path)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self):
        with self._db() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    event_key TEXT,
                    event_data TEXT NOT NULL,
                    decision TEXT,
                    created_at REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    from_node TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_rate_limits (
                    bucket TEXT PRIMARY KEY,
                    count INTEGER DEFAULT 0,
                    window_start REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_notes (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_conv_session ON agent_conversations(session_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON agent_events(event_type)")

    # ── Events ──

    def log_event(self, event_type: str, event_key: Optional[str],
                  event_data: dict, decision: Optional[dict] = None):
        with self._db() as conn:
            conn.execute(
                "INSERT INTO agent_events (event_type, event_key, event_data, decision, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (event_type, event_key, json.dumps(event_data),
                 json.dumps(decision) if decision else None, time.time())
            )

    def get_recent_events(self, limit: int = 20, event_type: Optional[str] = None) -> List[Dict]:
        with self._db() as conn:
            if event_type:
                rows = conn.execute(
                    "SELECT id, event_type, event_key, event_data, decision, created_at "
                    "FROM agent_events WHERE event_type = ? ORDER BY id DESC LIMIT ?",
                    (event_type, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, event_type, event_key, event_data, decision, created_at "
                    "FROM agent_events ORDER BY id DESC LIMIT ?",
                    (limit,)
                ).fetchall()
        return [
            {"id": r[0], "event_type": r[1], "event_key": r[2],
             "event_data": json.loads(r[3]), "decision": json.loads(r[4]) if r[4] else None,
             "created_at": r[5]}
            for r in rows
        ]

    def count_events_since(self, since: float, event_type: Optional[str] = None) -> int:
        with self._db() as conn:
            if event_type:
                row = conn.execute(
                    "SELECT COUNT(*) FROM agent_events WHERE created_at > ? AND event_type = ?",
                    (since, event_type)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM agent_events WHERE created_at > ?",
                    (since,)
                ).fetchone()
        return row[0] if row else 0

    # ── Conversations ──

    def add_conversation(self, session_id: str, from_node: str,
                         direction: str, body: str):
        with self._db() as conn:
            conn.execute(
                "INSERT INTO agent_conversations (session_id, from_node, direction, body, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, from_node, direction, body, time.time())
            )

    def get_conversation(self, session_id: str, limit: int = 10) -> List[Dict]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT from_node, direction, body, created_at "
                "FROM agent_conversations WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit)
            ).fetchall()
        return [
            {"from_node": r[0], "direction": r[1], "body": r[2], "created_at": r[3]}
            for r in reversed(rows)
        ]

    # ── State ──

    def get_state(self, key: str, default: str = "") -> str:
        with self._db() as conn:
            row = conn.execute(
                "SELECT value FROM agent_state WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else default

    def set_state(self, key: str, value: str):
        with self._db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO agent_state (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, time.time())
            )

    # ── Rate Limits ──

    def check_rate_limit(self, bucket: str, max_count: int, window_seconds: float = 3600) -> bool:
        """Returns True if under limit, False if exceeded."""
        now = time.time()
        with self._db() as conn:
            row = conn.execute(
                "SELECT count, window_start FROM agent_rate_limits WHERE bucket = ?",
                (bucket,)
            ).fetchone()
            if row is None or (now - row[1]) > window_seconds:
                conn.execute(
                    "INSERT OR REPLACE INTO agent_rate_limits (bucket, count, window_start) VALUES (?, 1, ?)",
                    (bucket, now)
                )
                return True
            if row[0] >= max_count:
                return False
            conn.execute(
                "UPDATE agent_rate_limits SET count = count + 1 WHERE bucket = ?",
                (bucket,)
            )
            return True

    def get_rate_count(self, bucket: str) -> int:
        with self._db() as conn:
            row = conn.execute(
                "SELECT count, window_start FROM agent_rate_limits WHERE bucket = ?",
                (bucket,)
            ).fetchone()
        if row is None:
            return 0
        if (time.time() - row[1]) > 3600:
            return 0
        return row[0]

    # ── Notes ──

    def set_note(self, key: str, value: str):
        now = time.time()
        with self._db() as conn:
            conn.execute(
                "INSERT INTO agent_notes (key, value, created_at, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, value, now, now)
            )

    def get_note(self, key: str) -> Optional[str]:
        with self._db() as conn:
            row = conn.execute(
                "SELECT value FROM agent_notes WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    def get_all_notes(self) -> List[Dict]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT key, value, updated_at FROM agent_notes ORDER BY updated_at DESC LIMIT 50"
            ).fetchall()
        return [{"key": r[0], "value": r[1], "updated_at": r[2]} for r in rows]
