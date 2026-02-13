"""Shared SQLite helpers for the skill-cache family of skills."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import sys
sys.path.insert(0, os.path.dirname(__file__))
from _cache_runtime import runtime_root

SCHEMA_VERSION = 1


def cache_db_path() -> Path:
    return runtime_root() / "skill-cache" / "cache.db"


def db_exists() -> bool:
    return cache_db_path().is_file()


def get_conn(readonly: bool = False) -> sqlite3.Connection:
    path = cache_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    uri = f"file:{path}{'?mode=ro' if readonly else ''}"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> str:
    """Create tables if missing. Returns action: 'created' or 'already_current'."""
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'")
    if cur.fetchone():
        cur.execute("SELECT version FROM schema_version LIMIT 1")
        row = cur.fetchone()
        if row and row[0] >= SCHEMA_VERSION:
            return "already_current"

    cur.executescript(_DDL)
    cur.execute("DELETE FROM schema_version")
    cur.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    conn.commit()
    return "created"


def reset_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        DROP TABLE IF EXISTS skill_runs;
        DROP TABLE IF EXISTS harvests;
        DROP TABLE IF EXISTS skills;
        DROP TABLE IF EXISTS schema_version;
    """)
    conn.commit()
    ensure_schema(conn)


def get_schema_version(conn: sqlite3.Connection) -> int:
    try:
        cur = conn.execute("SELECT version FROM schema_version LIMIT 1")
        row = cur.fetchone()
        return int(row[0]) if row else 0
    except sqlite3.OperationalError:
        return 0


def upsert_skill(
    conn: sqlite3.Connection,
    skill_name: str,
    node_id: str,
    host: str,
    port: int,
    sidecar_port: int,
    skill_sheet: Dict[str, Any],
    harvest_seq: int,
) -> None:
    now = time.time()
    conn.execute(
        """INSERT INTO skills
            (skill_name, node_id, host, port, sidecar_port,
             version, description, tags_json, input_schema_json, output_schema_json,
             price, max_input_size, first_seen_at, last_seen_at, harvest_seq, is_stale)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
           ON CONFLICT(skill_name, node_id) DO UPDATE SET
             host=excluded.host, port=excluded.port, sidecar_port=excluded.sidecar_port,
             version=excluded.version, description=excluded.description,
             tags_json=excluded.tags_json,
             input_schema_json=excluded.input_schema_json,
             output_schema_json=excluded.output_schema_json,
             price=excluded.price, max_input_size=excluded.max_input_size,
             last_seen_at=excluded.last_seen_at,
             harvest_seq=excluded.harvest_seq, is_stale=0
        """,
        (
            skill_name,
            node_id,
            host,
            port,
            sidecar_port,
            str(skill_sheet.get("version", "1.0.0")),
            str(skill_sheet.get("description", "")),
            json.dumps(skill_sheet.get("tags") or [], ensure_ascii=True),
            json.dumps(skill_sheet.get("input_schema") or {}, ensure_ascii=True, sort_keys=True),
            json.dumps(skill_sheet.get("output_schema") or {}, ensure_ascii=True, sort_keys=True),
            float(skill_sheet.get("price", 1.0)),
            int(skill_sheet.get("max_input_size", 65536)),
            now,  # first_seen_at (only used on INSERT)
            now,  # last_seen_at
            harvest_seq,
        ),
    )


def insert_run(
    conn: sqlite3.Connection,
    skill_name: str,
    input_data: Any,
    output_data: Any,
    source: str = "live",
    duration_ms: int = 0,
    error: str = "",
) -> int:
    cur = conn.execute(
        """INSERT INTO skill_runs
            (skill_name, input_json, output_json, source, duration_ms, captured_at, error)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            skill_name,
            json.dumps(input_data, ensure_ascii=True, sort_keys=True, default=str),
            json.dumps(output_data, ensure_ascii=True, sort_keys=True, default=str),
            source,
            duration_ms,
            time.time(),
            error,
        ),
    )
    conn.commit()
    return cur.lastrowid or 0


def get_best_run(
    conn: sqlite3.Connection,
    skill_name: str,
    input_json: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Find best matching run: exact input match first, then most recent success."""
    if input_json:
        cur = conn.execute(
            """SELECT * FROM skill_runs
               WHERE skill_name = ? AND input_json = ? AND error = ''
               ORDER BY captured_at DESC LIMIT 1""",
            (skill_name, input_json),
        )
        row = cur.fetchone()
        if row:
            return dict(row)

    cur = conn.execute(
        """SELECT * FROM skill_runs
           WHERE skill_name = ? AND error = ''
           ORDER BY captured_at DESC LIMIT 1""",
        (skill_name,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def get_skill_schema(
    conn: sqlite3.Connection,
    skill_name: str,
) -> Optional[Dict[str, Any]]:
    """Get the freshest, cheapest provider entry for a skill."""
    cur = conn.execute(
        """SELECT * FROM skills
           WHERE skill_name = ? AND is_stale = 0
           ORDER BY last_seen_at DESC, price ASC
           LIMIT 1""",
        (skill_name,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
_DDL = """
CREATE TABLE IF NOT EXISTS skills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name      TEXT NOT NULL,
    node_id         TEXT NOT NULL,
    host            TEXT NOT NULL DEFAULT '',
    port            INTEGER NOT NULL DEFAULT 0,
    sidecar_port    INTEGER NOT NULL DEFAULT 0,
    version         TEXT NOT NULL DEFAULT '1.0.0',
    description     TEXT NOT NULL DEFAULT '',
    tags_json       TEXT NOT NULL DEFAULT '[]',
    input_schema_json  TEXT NOT NULL DEFAULT '{}',
    output_schema_json TEXT NOT NULL DEFAULT '{}',
    price           REAL NOT NULL DEFAULT 1.0,
    max_input_size  INTEGER NOT NULL DEFAULT 65536,
    first_seen_at   REAL NOT NULL,
    last_seen_at    REAL NOT NULL,
    harvest_seq     INTEGER NOT NULL DEFAULT 0,
    is_stale        INTEGER NOT NULL DEFAULT 0,
    UNIQUE(skill_name, node_id)
);
CREATE INDEX IF NOT EXISTS idx_skills_name  ON skills(skill_name);
CREATE INDEX IF NOT EXISTS idx_skills_stale ON skills(is_stale);

CREATE TABLE IF NOT EXISTS skill_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name      TEXT NOT NULL,
    input_json      TEXT NOT NULL DEFAULT '{}',
    output_json     TEXT NOT NULL DEFAULT '{}',
    source          TEXT NOT NULL DEFAULT 'live',
    duration_ms     INTEGER NOT NULL DEFAULT 0,
    captured_at     REAL NOT NULL,
    error           TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_runs_skill ON skill_runs(skill_name);

CREATE TABLE IF NOT EXISTS harvests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      REAL NOT NULL,
    finished_at     REAL NOT NULL,
    total_found     INTEGER NOT NULL DEFAULT 0,
    upserted        INTEGER NOT NULL DEFAULT 0,
    marked_stale    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);
"""
