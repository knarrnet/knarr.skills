"""Walk the DHT network, upsert all discovered skills into the local cache."""

from __future__ import annotations

import os
import sys
import time
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))
from _common import SkillError, ensure_flat_str_dict, error_result, parse_int
from skill_cache_db import db_exists, get_conn, ensure_schema, upsert_skill

NODE: Any = None


def set_node(node: Any) -> None:
    global NODE
    NODE = node


async def handle(input_data: dict, ctx=None) -> dict:
    try:
        if NODE is None:
            raise SkillError("Skill not initialized with node context")

        t0 = time.time()

        purge_stale_after = parse_int(
            str(input_data.get("purge_stale_after") or "5"), 5, 0, 1000,
        )

        # --- Ensure DB ---
        conn = get_conn()
        try:
            ensure_schema(conn)

            # --- Determine harvest sequence ---
            cur = conn.execute("SELECT COALESCE(MAX(harvest_seq), 0) FROM skills")
            harvest_seq = cur.fetchone()[0] + 1

            # --- Harvest local skills (all visibilities) ---
            upserted = 0
            seen_pairs = set()
            local_count = 0

            own_skills = getattr(NODE, "_own_skills", {}) or {}
            own_vis = getattr(NODE, "_skill_visibility", {}) or {}
            local_id = getattr(NODE, "node_info", None)
            local_node_id = local_id.node_id if local_id else "local"
            local_host = local_id.host if local_id else "127.0.0.1"
            local_port = local_id.port if local_id else 0
            local_sidecar = getattr(NODE, "_sidecar_port", 0) or 0

            for skill_name, skill_sheet in own_skills.items():
                try:
                    name = skill_name.strip().lower()
                    if not name:
                        continue
                    pair = (name, local_node_id)
                    seen_pairs.add(pair)

                    sheet_dict = skill_sheet.to_dict() if hasattr(skill_sheet, "to_dict") else {}
                    sheet_dict["_visibility"] = own_vis.get(name, "public")

                    upsert_skill(conn, name, local_node_id, local_host, local_port,
                                 local_sidecar, sheet_dict, harvest_seq)
                    upserted += 1
                    local_count += 1
                except Exception:
                    continue

            # --- Query DHT for remote skills ---
            results = await NODE.query("all", "")
            network_found = len(results)

            for entry in results:
                try:
                    node_id = str(entry.get("node_id") or "").strip()
                    host = str(entry.get("host") or "").strip()
                    port = int(entry.get("port") or 0)
                    sidecar_port = int(entry.get("sidecar_port") or 0)
                    sheet = entry.get("skill_sheet") or {}
                    name = str(sheet.get("name") or "").strip().lower()

                    if not name or not node_id:
                        continue

                    pair = (name, node_id)
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)

                    upsert_skill(conn, name, node_id, host, port, sidecar_port, sheet, harvest_seq)
                    upserted += 1
                except Exception:
                    continue

            total_found = local_count + network_found
            conn.commit()

            # --- Mark stale ---
            cur = conn.execute(
                "UPDATE skills SET is_stale = 1 WHERE harvest_seq < ? AND is_stale = 0",
                (harvest_seq,),
            )
            newly_stale = cur.rowcount
            conn.commit()

            # --- Purge old stale ---
            purged = 0
            if purge_stale_after > 0:
                cutoff = harvest_seq - purge_stale_after
                if cutoff > 0:
                    cur = conn.execute(
                        "DELETE FROM skills WHERE is_stale = 1 AND harvest_seq < ?",
                        (cutoff,),
                    )
                    purged = cur.rowcount
                    conn.commit()

            # --- Unique skill count ---
            cur = conn.execute("SELECT COUNT(DISTINCT skill_name) FROM skills WHERE is_stale = 0")
            unique_skills = cur.fetchone()[0]

            # --- Record harvest ---
            finished = time.time()
            cur = conn.execute(
                """INSERT INTO harvests (started_at, finished_at, total_found, upserted, marked_stale)
                   VALUES (?, ?, ?, ?, ?)""",
                (t0, finished, total_found, upserted, newly_stale),
            )
            harvest_id = cur.lastrowid
            conn.commit()

            duration_ms = int((finished - t0) * 1000)

            return ensure_flat_str_dict({
                "status": "ok",
                "harvest_id": str(harvest_id),
                "harvest_seq": str(harvest_seq),
                "total_found": str(total_found),
                "local_skills": str(local_count),
                "network_found": str(network_found),
                "unique_skills": str(unique_skills),
                "upserted": str(upserted),
                "newly_stale": str(newly_stale),
                "purged": str(purged),
                "duration_ms": str(duration_ms),
            })
        finally:
            conn.close()

    except SkillError as exc:
        return error_result(str(exc))
    except Exception as exc:
        return error_result(str(exc))
