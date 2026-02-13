"""Create, migrate, or reset the local skill cache SQLite database."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from _common import SkillError, ensure_flat_str_dict, error_result
from skill_cache_db import (
    cache_db_path, db_exists, get_conn, ensure_schema,
    reset_schema, get_schema_version, SCHEMA_VERSION,
)


async def handle(input_data: dict, ctx=None) -> dict:
    try:
        action = str(input_data.get("action") or "init").strip().lower()
        if action not in ("init", "reset", "status"):
            raise SkillError(f"Invalid action '{action}'. Use: init, reset, status")

        db_path = cache_db_path()

        if action == "status":
            if not db_exists():
                return ensure_flat_str_dict({
                    "status": "ok",
                    "db_path": str(db_path),
                    "schema_version": "0",
                    "action_taken": "not_initialized",
                    "table_count": "0",
                })
            conn = get_conn(readonly=True)
            try:
                ver = get_schema_version(conn)
                cur = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'")
                tc = cur.fetchone()[0]
                return ensure_flat_str_dict({
                    "status": "ok",
                    "db_path": str(db_path),
                    "schema_version": str(ver),
                    "action_taken": "status",
                    "table_count": str(tc),
                })
            finally:
                conn.close()

        conn = get_conn()
        try:
            if action == "reset":
                reset_schema(conn)
                return ensure_flat_str_dict({
                    "status": "ok",
                    "db_path": str(db_path),
                    "schema_version": str(SCHEMA_VERSION),
                    "action_taken": "reset",
                })

            # action == "init"
            taken = ensure_schema(conn)
            return ensure_flat_str_dict({
                "status": "ok",
                "db_path": str(db_path),
                "schema_version": str(SCHEMA_VERSION),
                "action_taken": taken,
            })
        finally:
            conn.close()

    except SkillError as exc:
        return error_result(str(exc))
    except Exception as exc:
        return error_result(str(exc))
