"""Thrall Switchboard — Database layer.

Tables:
- thrall_journal: every pipeline execution (audit + training + dryrun)
- thrall_context: async workflow state (session continuations, flags)
- thrall_recipes: runtime cache of loaded recipe configs
- thrall_wallet: delegated spending ledger (settlement identity)
- thrall_memory: classified decision memory (skill, node_id, outcome)
"""

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


class ThrallDB:
    def __init__(self, db_path: str):
        self._path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    def _migrate(self):
        c = self._conn
        c.executescript("""
            CREATE TABLE IF NOT EXISTS thrall_journal (
                id              INTEGER PRIMARY KEY,
                timestamp       REAL NOT NULL,
                pipeline        TEXT NOT NULL,
                session_id      TEXT,
                envelope_json   TEXT NOT NULL,
                filter_json     TEXT,
                eval_type       TEXT,
                eval_result     TEXT,
                action_name     TEXT,
                action_trace    TEXT,
                context_written TEXT,
                wall_ms         INTEGER,
                mode            TEXT DEFAULT 'automated',
                reviewed        INTEGER DEFAULT 0,
                correction      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_journal_pipeline ON thrall_journal(pipeline);
            CREATE INDEX IF NOT EXISTS idx_journal_ts ON thrall_journal(timestamp);
            CREATE INDEX IF NOT EXISTS idx_journal_review ON thrall_journal(reviewed);

            CREATE TABLE IF NOT EXISTS thrall_context (
                session_id  TEXT NOT NULL,
                key         TEXT NOT NULL,
                value       TEXT,
                created_at  REAL NOT NULL,
                expires_at  REAL,
                PRIMARY KEY (session_id, key)
            );
            CREATE INDEX IF NOT EXISTS idx_context_expires ON thrall_context(expires_at);

            CREATE TABLE IF NOT EXISTS thrall_recipes (
                name        TEXT PRIMARY KEY,
                config_json TEXT NOT NULL,
                source_file TEXT,
                loaded_at   REAL NOT NULL,
                mode        TEXT DEFAULT 'automated'
            );

            CREATE TABLE IF NOT EXISTS thrall_compilation (
                id          INTEGER PRIMARY KEY,
                buffer_name TEXT NOT NULL,
                entry_json  TEXT NOT NULL,
                pipeline    TEXT NOT NULL,
                created_at  REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_comp_buffer ON thrall_compilation(buffer_name);

            CREATE TABLE IF NOT EXISTS thrall_wallet (
                id          INTEGER PRIMARY KEY,
                timestamp   REAL NOT NULL,
                amount      REAL NOT NULL,
                reference   TEXT,
                peer_pk     TEXT,
                description TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_wallet_ts ON thrall_wallet(timestamp);

            CREATE TABLE IF NOT EXISTS thrall_memory (
                id            INTEGER PRIMARY KEY,
                timestamp     REAL NOT NULL,
                skill         TEXT NOT NULL,
                node_id       TEXT NOT NULL DEFAULT '',
                mail_id       TEXT NOT NULL DEFAULT '',
                outcome       TEXT NOT NULL,
                amount        REAL DEFAULT 0.0,
                reasoning     TEXT DEFAULT '',
                metadata_json TEXT,
                dryrun        INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_memory_skill ON thrall_memory(skill);
            CREATE INDEX IF NOT EXISTS idx_memory_node ON thrall_memory(node_id);
            CREATE INDEX IF NOT EXISTS idx_memory_ts ON thrall_memory(timestamp);
        """)

        # Migration: add from_node column for fast rate-limit / cache queries
        try:
            c.execute("ALTER TABLE thrall_journal ADD COLUMN from_node TEXT DEFAULT ''")
            c.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
        c.commit()

    # ── Journal ──

    def write_journal(self, pipeline: str, envelope: dict, filter_result: dict,
                      eval_type: str, eval_result: str, action_name: str,
                      action_trace: str, context_written: dict,
                      wall_ms: int, mode: str, session_id: str = None) -> int:
        from_node = envelope.get("from_node", "")[:16]
        cur = self._conn.execute("""
            INSERT INTO thrall_journal
                (timestamp, pipeline, session_id, envelope_json, filter_json,
                 eval_type, eval_result, action_name, action_trace,
                 context_written, wall_ms, mode, from_node)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            time.time(), pipeline, session_id,
            json.dumps(envelope), json.dumps(filter_result),
            eval_type, eval_result, action_name, action_trace,
            json.dumps(context_written) if context_written else None,
            wall_ms, mode, from_node,
        ))
        self._conn.commit()
        return cur.lastrowid

    # ── Rate-limit & LLM cache queries ──

    def count_recent_from_sender(self, from_node_prefix: str, pipeline: str,
                                  window_seconds: float) -> int:
        """Count journal entries from a sender within a time window."""
        cutoff = time.time() - window_seconds
        row = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM thrall_journal "
            "WHERE from_node = ? AND pipeline = ? AND timestamp > ?",
            (from_node_prefix[:16], pipeline, cutoff),
        ).fetchone()
        return row["cnt"] if row else 0

    def get_cached_eval(self, from_node_prefix: str, pipeline: str,
                        ttl_seconds: float) -> Optional[dict]:
        """Find the most recent LLM eval result for a sender within TTL.

        Returns {"action": "...", "reason": "..."} or None.
        """
        cutoff = time.time() - ttl_seconds
        row = self._conn.execute(
            "SELECT action_name, eval_result, timestamp FROM thrall_journal "
            "WHERE from_node = ? AND pipeline = ? AND eval_type = 'llm' "
            "AND timestamp > ? ORDER BY id DESC LIMIT 1",
            (from_node_prefix[:16], pipeline, cutoff),
        ).fetchone()
        if not row:
            return None
        return {
            "action": row["action_name"],
            "eval_result": row["eval_result"],
            "timestamp": row["timestamp"],
        }

    def query_journal(self, pipeline: str = None, reviewed: int = None,
                      limit: int = 50, since: float = None) -> List[dict]:
        sql = "SELECT * FROM thrall_journal WHERE 1=1"
        params = []
        if pipeline:
            sql += " AND pipeline = ?"
            params.append(pipeline)
        if reviewed is not None:
            sql += " AND reviewed = ?"
            params.append(reviewed)
        if since:
            sql += " AND timestamp > ?"
            params.append(since)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def mark_reviewed(self, journal_id: int, reviewed: int, correction: str = None):
        self._conn.execute(
            "UPDATE thrall_journal SET reviewed=?, correction=? WHERE id=?",
            (reviewed, correction, journal_id))
        self._conn.commit()

    # ── Context ──

    def set_context(self, session_id: str, key: str, value: str,
                    ttl_seconds: float = None):
        expires = time.time() + ttl_seconds if ttl_seconds else None
        self._conn.execute("""
            INSERT OR REPLACE INTO thrall_context (session_id, key, value, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
        """, (session_id, key, value, time.time(), expires))
        self._conn.commit()

    def get_context(self, session_id: str, key: str = None) -> dict:
        if key:
            row = self._conn.execute(
                "SELECT value FROM thrall_context WHERE session_id=? AND key=? AND (expires_at IS NULL OR expires_at > ?)",
                (session_id, key, time.time())).fetchone()
            return {"value": row["value"]} if row else {}
        rows = self._conn.execute(
            "SELECT key, value FROM thrall_context WHERE session_id=? AND (expires_at IS NULL OR expires_at > ?)",
            (session_id, time.time())).fetchall()
        return {r["key"]: r["value"] for r in rows}

    def clear_context(self, session_id: str, key: str = None):
        if key:
            self._conn.execute(
                "DELETE FROM thrall_context WHERE session_id=? AND key=?",
                (session_id, key))
        else:
            self._conn.execute(
                "DELETE FROM thrall_context WHERE session_id=?", (session_id,))
        self._conn.commit()

    def cleanup_expired_context(self) -> int:
        cur = self._conn.execute(
            "DELETE FROM thrall_context WHERE expires_at IS NOT NULL AND expires_at < ?",
            (time.time(),))
        self._conn.commit()
        return cur.rowcount

    # ── Recipes ──

    def upsert_recipe(self, name: str, config: dict, source_file: str = None,
                      mode: str = "automated"):
        self._conn.execute("""
            INSERT OR REPLACE INTO thrall_recipes (name, config_json, source_file, loaded_at, mode)
            VALUES (?, ?, ?, ?, ?)
        """, (name, json.dumps(config), source_file, time.time(), mode))
        self._conn.commit()

    def get_recipe(self, name: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM thrall_recipes WHERE name=?", (name,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["config"] = json.loads(d["config_json"])
        return d

    def get_all_recipes(self) -> List[dict]:
        rows = self._conn.execute("SELECT * FROM thrall_recipes").fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["config"] = json.loads(d["config_json"])
            result.append(d)
        return result

    def prune_recipes(self, keep_names: List[str]) -> int:
        """Delete DB rows for recipes not in keep_names. Returns count pruned."""
        if not keep_names:
            return 0
        placeholders = ",".join("?" for _ in keep_names)
        cur = self._conn.execute(
            f"DELETE FROM thrall_recipes WHERE name NOT IN ({placeholders})",
            keep_names)
        self._conn.commit()
        return cur.rowcount

    # ── Compilation buffer ──

    def add_to_buffer(self, buffer_name: str, entry: dict, pipeline: str) -> int:
        cur = self._conn.execute("""
            INSERT INTO thrall_compilation (buffer_name, entry_json, pipeline, created_at)
            VALUES (?, ?, ?, ?)
        """, (buffer_name, json.dumps(entry), pipeline, time.time()))
        self._conn.commit()
        return cur.lastrowid

    def get_buffer(self, buffer_name: str) -> List[dict]:
        rows = self._conn.execute(
            "SELECT * FROM thrall_compilation WHERE buffer_name=? ORDER BY created_at",
            (buffer_name,)).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["entry"] = json.loads(d["entry_json"])
            result.append(d)
        return result

    def buffer_count(self, buffer_name: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM thrall_compilation WHERE buffer_name=?",
            (buffer_name,)).fetchone()
        return row["cnt"]

    def flush_buffer(self, buffer_name: str) -> List[dict]:
        entries = self.get_buffer(buffer_name)
        self._conn.execute(
            "DELETE FROM thrall_compilation WHERE buffer_name=?", (buffer_name,))
        self._conn.commit()
        return entries

    # ── Wallet ──

    def record_wallet_spend(self, amount: float, reference: str = "",
                            peer_pk: str = "", description: str = ""):
        self._conn.execute("""
            INSERT INTO thrall_wallet (timestamp, amount, reference, peer_pk, description)
            VALUES (?, ?, ?, ?, ?)
        """, (time.time(), amount, reference, peer_pk, description))
        self._conn.commit()

    def get_daily_spend(self, day_start: float = None) -> float:
        """Sum of wallet spending since day_start (default: midnight UTC today)."""
        if day_start is None:
            import calendar
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            day_start = calendar.timegm(now.replace(
                hour=0, minute=0, second=0, microsecond=0).timetuple())
        row = self._conn.execute(
            "SELECT COALESCE(SUM(amount), 0.0) as total FROM thrall_wallet "
            "WHERE timestamp >= ?", (day_start,)
        ).fetchone()
        return row["total"]

    def get_wallet_history(self, since: float = None, limit: int = 50) -> List[dict]:
        """Return wallet spending records since timestamp (default: last 24h)."""
        if since is None:
            since = time.time() - 86400
        rows = self._conn.execute(
            "SELECT * FROM thrall_wallet WHERE timestamp >= ? "
            "ORDER BY id DESC LIMIT ?", (since, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Memory ──

    def record_memory(self, skill: str, node_id: str, outcome: str,
                      mail_id: str = "", amount: float = 0.0,
                      reasoning: str = "", metadata: dict = None,
                      dryrun: bool = False) -> int:
        cur = self._conn.execute("""
            INSERT INTO thrall_memory
                (timestamp, skill, node_id, mail_id, outcome,
                 amount, reasoning, metadata_json, dryrun)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            time.time(), skill, node_id[:64], mail_id, outcome,
            amount, reasoning,
            json.dumps(metadata) if metadata else None,
            1 if dryrun else 0,
        ))
        self._conn.commit()
        return cur.lastrowid

    def query_memory(self, node_id: str = None, skill: str = None,
                     outcome: str = None, limit: int = 10,
                     since: float = None,
                     include_dryrun: bool = False) -> List[dict]:
        sql = "SELECT * FROM thrall_memory WHERE 1=1"
        params: list = []
        if not include_dryrun:
            sql += " AND dryrun = 0"
        if node_id:
            sql += " AND node_id = ?"
            params.append(node_id[:64])
        if skill:
            sql += " AND skill = ?"
            params.append(skill)
        if outcome:
            sql += " AND outcome = ?"
            params.append(outcome)
        if since:
            sql += " AND timestamp > ?"
            params.append(since)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if d.get("metadata_json"):
                d["metadata"] = json.loads(d["metadata_json"])
            result.append(d)
        return result

    # ── Stats ──

    def get_stats(self, since: float, pipeline: str = None) -> list:
        """Aggregate pipeline stats for the agent.

        Returns list of dicts with action_name, eval_type, count,
        avg_wall_ms, llm_ms_total — grouped by action+eval_type.
        """
        where = "WHERE timestamp > ?"
        params: list = [since]
        if pipeline:
            where += " AND pipeline = ?"
            params.append(pipeline)

        rows = self._conn.execute(f"""
            SELECT
                action_name,
                eval_type,
                COUNT(*) as count,
                CAST(AVG(wall_ms) AS INTEGER) as avg_wall_ms,
                SUM(CASE WHEN eval_type = 'llm' THEN wall_ms ELSE 0 END) as llm_ms_total
            FROM thrall_journal
            {where}
            GROUP BY action_name, eval_type
            ORDER BY count DESC
        """, params).fetchall()
        return [dict(r) for r in rows]

    def get_totals(self, since: float) -> dict:
        """Quick totals for stats summary line."""
        row = self._conn.execute(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN eval_type='llm' THEN 1 ELSE 0 END) as llm_calls, "
            "SUM(CASE WHEN eval_type='cache' THEN 1 ELSE 0 END) as cache_hits, "
            "SUM(CASE WHEN eval_type='bypass' THEN 1 ELSE 0 END) as bypasses, "
            "SUM(CASE WHEN eval_type='hotwire' THEN 1 ELSE 0 END) as hotwire, "
            "SUM(CASE WHEN eval_type='rate_limit' THEN 1 ELSE 0 END) as rate_limited "
            "FROM thrall_journal WHERE timestamp > ?", (since,)
        ).fetchone()
        return dict(row) if row else {}

    def close(self):
        self._conn.close()
