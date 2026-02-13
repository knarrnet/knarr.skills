"""Report skill cache health: counts, coverage, harvest history, DB size."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))
from _common import SkillError, ensure_flat_str_dict, error_result, to_json_string
from skill_cache_db import cache_db_path, db_exists, get_conn, get_schema_version


async def handle(input_data: dict, ctx=None) -> dict:
    try:
        if not db_exists():
            raise SkillError("Skill cache not initialized. Run skill-cache-init-lite first.")

        action = str(input_data.get("action") or "summary").strip().lower()
        if action not in ("summary", "detail"):
            raise SkillError(f"Invalid action '{action}'. Use: summary, detail")

        db_path = cache_db_path()
        db_size = db_path.stat().st_size if db_path.is_file() else 0

        conn = get_conn(readonly=True)
        try:
            # Total rows
            total_skills = conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
            unique_skills = conn.execute("SELECT COUNT(DISTINCT skill_name) FROM skills").fetchone()[0]
            fresh_skills = conn.execute(
                "SELECT COUNT(DISTINCT skill_name) FROM skills WHERE is_stale = 0"
            ).fetchone()[0]
            stale_skills = unique_skills - fresh_skills

            # Demo coverage
            skills_with_demos = conn.execute(
                """SELECT COUNT(DISTINCT sr.skill_name) FROM skill_runs sr
                   JOIN skills s ON s.skill_name = sr.skill_name
                   WHERE sr.error = '' AND s.is_stale = 0"""
            ).fetchone()[0]
            skills_without_demos = fresh_skills - skills_with_demos
            coverage_pct = round((skills_with_demos / fresh_skills * 100), 1) if fresh_skills > 0 else 0.0

            # Runs
            total_runs = conn.execute("SELECT COUNT(*) FROM skill_runs").fetchone()[0]

            # Harvest history
            harvest_count = conn.execute("SELECT COUNT(*) FROM harvests").fetchone()[0]
            last_harvest = conn.execute(
                "SELECT * FROM harvests ORDER BY id DESC LIMIT 1"
            ).fetchone()

            last_harvest_at = ""
            last_harvest_id = "0"
            if last_harvest:
                last_harvest_id = str(last_harvest["id"])
                last_harvest_at = datetime.fromtimestamp(
                    last_harvest["finished_at"], tz=timezone.utc
                ).isoformat()

            result = {
                "status": "ok",
                "total_skill_rows": str(total_skills),
                "unique_skills": str(unique_skills),
                "fresh_skills": str(fresh_skills),
                "stale_skills": str(stale_skills),
                "skills_with_demos": str(skills_with_demos),
                "skills_without_demos": str(skills_without_demos),
                "coverage_pct": str(coverage_pct),
                "total_runs": str(total_runs),
                "last_harvest_at": last_harvest_at,
                "last_harvest_id": last_harvest_id,
                "harvest_count": str(harvest_count),
                "db_size_bytes": str(db_size),
                "schema_version": str(get_schema_version(conn)),
            }

            if action == "detail":
                cur = conn.execute("""
                    SELECT
                        s.skill_name,
                        COUNT(DISTINCT s.node_id) AS provider_count,
                        COALESCE(sr.run_count, 0) AS run_count,
                        MAX(s.last_seen_at) AS last_seen_at,
                        MIN(s.is_stale) AS is_stale
                    FROM skills s
                    LEFT JOIN (
                        SELECT skill_name, COUNT(*) AS run_count
                        FROM skill_runs WHERE error = ''
                        GROUP BY skill_name
                    ) sr ON sr.skill_name = s.skill_name
                    GROUP BY s.skill_name
                    ORDER BY s.skill_name
                """)
                detail = []
                for row in cur.fetchall():
                    detail.append({
                        "name": row["skill_name"],
                        "provider_count": row["provider_count"],
                        "run_count": row["run_count"],
                        "last_seen_at": row["last_seen_at"],
                        "is_stale": bool(row["is_stale"]),
                    })
                result["detail_json"] = to_json_string(detail)

            return ensure_flat_str_dict(result)
        finally:
            conn.close()

    except SkillError as exc:
        return error_result(str(exc))
    except Exception as exc:
        return error_result(str(exc))
