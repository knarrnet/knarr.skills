"""Search the local skill cache by name, tag, text, schema keys, or price.

Auto-refreshes when the cache is older than max_age (default 5 minutes).
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(__file__))
from _common import SkillError, ensure_flat_str_dict, error_result, parse_int, to_json_string
from skill_cache_db import db_exists, get_conn, ensure_schema

NODE: Any = None
DEFAULT_MAX_AGE = 300  # 5 minutes


def set_node(node: Any) -> None:
    global NODE
    NODE = node


async def _auto_refresh(max_age: int) -> str:
    """Re-harvest if cache is stale. Returns 'refreshed' or 'fresh'."""
    if NODE is None:
        return "no_node"
    conn = get_conn(readonly=True)
    try:
        cur = conn.execute("SELECT MAX(finished_at) FROM harvests")
        row = cur.fetchone()
        last = row[0] if row and row[0] else 0
    except Exception:
        last = 0
    finally:
        conn.close()

    if time.time() - last < max_age:
        return "fresh"

    # Trigger harvest via call_local
    try:
        await NODE.call_local("skill-cache-harvest-lite", {})
        return "refreshed"
    except Exception:
        return "refresh_failed"


async def handle(input_data: dict, ctx=None) -> dict:
    try:
        # Auto-init if DB missing
        if not db_exists():
            if NODE is not None:
                try:
                    await NODE.call_local("skill-cache-init-lite", {"action": "init"})
                    await NODE.call_local("skill-cache-harvest-lite", {})
                except Exception:
                    pass
            if not db_exists():
                raise SkillError("Skill cache not initialized. Run skill-cache-init-lite first.")

        # Auto-refresh if stale
        max_age = parse_int(str(input_data.get("max_age") or str(DEFAULT_MAX_AGE)), DEFAULT_MAX_AGE, 0, 86400)
        if max_age > 0:
            await _auto_refresh(max_age)

        t0 = time.time()

        q = str(input_data.get("q") or "").strip()
        name = str(input_data.get("name") or "").strip().lower()
        tag = str(input_data.get("tag") or "").strip().lower()
        has_input = str(input_data.get("has_input") or "").strip()
        has_output = str(input_data.get("has_output") or "").strip()
        max_price_raw = str(input_data.get("max_price") or "").strip()
        include_stale = str(input_data.get("include_stale") or "false").lower() in ("true", "1", "yes")
        max_results = parse_int(str(input_data.get("max_results") or "50"), 50, 1, 500)

        conn = get_conn(readonly=True)
        try:
            # Build query: deduplicate by skill_name, pick freshest+cheapest
            # Use a CTE to get one row per skill_name
            where_clauses = []
            params: List[Any] = []

            if not include_stale:
                where_clauses.append("s.is_stale = 0")

            if name:
                where_clauses.append("s.skill_name = ?")
                params.append(name)

            if tag:
                where_clauses.append("LOWER(s.tags_json) LIKE ?")
                params.append(f'%"{tag}"%')

            if q:
                like_q = f"%{q.lower()}%"
                where_clauses.append(
                    "(LOWER(s.skill_name) LIKE ? OR LOWER(s.description) LIKE ? OR LOWER(s.tags_json) LIKE ?)"
                )
                params.extend([like_q, like_q, like_q])

            if has_input:
                where_clauses.append("LOWER(s.input_schema_json) LIKE ?")
                params.append(f'%"{has_input.lower()}"%')

            if has_output:
                where_clauses.append("LOWER(s.output_schema_json) LIKE ?")
                params.append(f'%"{has_output.lower()}"%')

            if max_price_raw:
                try:
                    max_price = float(max_price_raw)
                    where_clauses.append("s.price <= ?")
                    params.append(max_price)
                except ValueError:
                    pass

            where_sql = (" AND ".join(where_clauses)) if where_clauses else "1=1"

            # Deduplicated query: one row per skill_name, freshest+cheapest
            sql = f"""
                WITH ranked AS (
                    SELECT s.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY s.skill_name
                               ORDER BY s.last_seen_at DESC, s.price ASC
                           ) AS rn
                    FROM skills s
                    WHERE {where_sql}
                )
                SELECT r.*,
                       (SELECT COUNT(*) FROM skills s2
                        WHERE s2.skill_name = r.skill_name AND s2.is_stale = 0) AS provider_count,
                       (SELECT COUNT(*) FROM skill_runs sr
                        WHERE sr.skill_name = r.skill_name AND sr.error = '') AS run_count
                FROM ranked r
                WHERE r.rn = 1
                ORDER BY r.skill_name
                LIMIT ?
            """
            params.append(max_results)

            cur = conn.execute(sql, params)
            rows = cur.fetchall()

            skills = []
            for row in rows:
                skills.append({
                    "name": row["skill_name"],
                    "version": row["version"],
                    "description": row["description"],
                    "tags": json.loads(row["tags_json"]),
                    "input_schema": json.loads(row["input_schema_json"]),
                    "output_schema": json.loads(row["output_schema_json"]),
                    "price": row["price"],
                    "provider_count": row["provider_count"],
                    "has_demo_data": "true" if row["run_count"] > 0 else "false",
                    "last_seen_at": row["last_seen_at"],
                })

            # Cache age
            cur2 = conn.execute("SELECT MAX(finished_at) FROM harvests")
            last_harvest = cur2.fetchone()
            cache_age = int(time.time() - (last_harvest[0] if last_harvest and last_harvest[0] else 0))

            query_ms = int((time.time() - t0) * 1000)

            return ensure_flat_str_dict({
                "status": "ok",
                "count": str(len(skills)),
                "skills_json": to_json_string(skills),
                "query_ms": str(query_ms),
                "cache_age_secs": str(cache_age),
            })
        finally:
            conn.close()

    except SkillError as exc:
        return error_result(str(exc))
    except Exception as exc:
        return error_result(str(exc))
