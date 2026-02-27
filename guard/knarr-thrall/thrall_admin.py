"""thrall-prompt-load — Operator-only skill for managing thrall prompts.

Push new classification prompts to thrall's local DB. Only the operator
node (whitelisted in knarr.toml) can call this skill.

Uses module-level singleton. Receives the DB connection from the guard
plugin (same connection, same thread — no write-write contention).
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
