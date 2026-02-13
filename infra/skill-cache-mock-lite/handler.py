"""Mock-execute a cached skill: replay recorded output, generate synthetic, or live passthrough."""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict

sys.path.insert(0, os.path.dirname(__file__))
from _common import SkillError, ensure_flat_str_dict, error_result, to_json_string
from skill_cache_db import db_exists, get_conn, get_best_run, get_skill_schema, insert_run

NODE: Any = None


def set_node(node: Any) -> None:
    global NODE
    NODE = node


def _synthetic_value(key: str) -> str:
    """Generate a plausible placeholder from the output schema key name."""
    k = key.lower()
    if "status" in k:
        return "ok"
    if "error" in k:
        return ""
    if "count" in k or "total" in k:
        return "1"
    if "score" in k or "pct" in k or "percent" in k or "confidence" in k:
        return "0.75"
    if "hash" in k or "asset" in k:
        return "mock_asset_0000000000000000000000000000000000000000000000000000000000000000"
    if "ext" in k:
        return "wav"
    if "bytes" in k or "size" in k:
        return "1024"
    if "path" in k or "url" in k:
        return "https://example.com/mock"
    if k.endswith("_json") or "json" in k:
        return "[]"
    if "ms" in k or "latency" in k or "duration" in k:
        return "50"
    if "engine" in k or "model" in k:
        return "mock"
    if "voice" in k or "speaker" in k:
        return "mock_voice"
    if "text" in k or "content" in k or "description" in k:
        return "Mock output text for testing."
    if "title" in k or "name" in k:
        return "Mock Title"
    return "mock_value"


def _generate_synthetic(output_schema: Dict[str, str]) -> Dict[str, str]:
    """Generate synthetic output from schema keys."""
    result: Dict[str, str] = {}
    for key in output_schema:
        result[key] = _synthetic_value(key)
    if not result:
        result["status"] = "ok"
        result["mock_note"] = "No output_schema available; returning minimal synthetic output"
    return result


async def handle(input_data: dict, ctx=None) -> dict:
    try:
        skill_name = str(input_data.get("skill") or "").strip().lower()
        if not skill_name:
            raise SkillError("Missing required field: skill")

        input_json_raw = str(input_data.get("input_json") or "{}").strip()
        mode = str(input_data.get("mode") or "auto").strip().lower()
        record_run = str(input_data.get("record_run") or "true").lower() in ("true", "1", "yes")

        if mode not in ("auto", "replay", "synthetic", "live"):
            raise SkillError(f"Invalid mode '{mode}'. Use: auto, replay, synthetic, live")

        try:
            input_dict = json.loads(input_json_raw)
        except json.JSONDecodeError:
            raise SkillError("input_json is not valid JSON")

        if not db_exists():
            raise SkillError("Skill cache not initialized. Run skill-cache-init-lite first.")

        conn = get_conn(readonly=(mode != "live"))
        try:
            # --- Replay mode ---
            if mode in ("auto", "replay"):
                run = get_best_run(conn, skill_name, input_json_raw)
                if run:
                    return ensure_flat_str_dict({
                        "status": "ok",
                        "mock_mode": "replay",
                        "output_json": run["output_json"],
                        "skill": skill_name,
                        "confidence": "high",
                        "latency_ms": str(run.get("duration_ms") or 50),
                        "run_id": str(run["id"]),
                    })
                if mode == "replay":
                    return ensure_flat_str_dict({
                        "status": "no_demo_data",
                        "mock_mode": "replay",
                        "output_json": "{}",
                        "skill": skill_name,
                        "confidence": "none",
                        "latency_ms": "0",
                    })

            # --- Synthetic mode ---
            if mode in ("auto", "synthetic"):
                skill_row = get_skill_schema(conn, skill_name)
                if skill_row:
                    output_schema = json.loads(skill_row["output_schema_json"])
                    synthetic = _generate_synthetic(output_schema)
                    return ensure_flat_str_dict({
                        "status": "ok",
                        "mock_mode": "synthetic",
                        "output_json": to_json_string(synthetic),
                        "skill": skill_name,
                        "confidence": "medium",
                        "latency_ms": "1",
                    })
                if mode == "synthetic":
                    return ensure_flat_str_dict({
                        "status": "skill_not_found",
                        "mock_mode": "synthetic",
                        "output_json": "{}",
                        "skill": skill_name,
                        "confidence": "none",
                        "latency_ms": "0",
                    })

            # --- Live passthrough ---
            if mode in ("auto", "live"):
                if NODE is None:
                    raise SkillError("NODE not available for live passthrough")

                t0 = time.time()
                try:
                    result = await NODE.call_local(skill_name, input_dict)
                except KeyError:
                    return ensure_flat_str_dict({
                        "status": "skill_not_found",
                        "mock_mode": "live",
                        "output_json": "{}",
                        "skill": skill_name,
                        "confidence": "none",
                        "latency_ms": "0",
                    })

                duration_ms = int((time.time() - t0) * 1000)
                error_str = str(result.get("error", ""))

                if record_run:
                    w_conn = get_conn(readonly=False)
                    try:
                        insert_run(
                            w_conn, skill_name, input_dict, result,
                            source="live", duration_ms=duration_ms,
                            error=error_str if error_str else "",
                        )
                    finally:
                        w_conn.close()

                return ensure_flat_str_dict({
                    "status": "ok" if not error_str else "error",
                    "mock_mode": "live",
                    "output_json": to_json_string(result),
                    "skill": skill_name,
                    "confidence": "live",
                    "latency_ms": str(duration_ms),
                })

            return ensure_flat_str_dict({
                "status": "skill_not_found",
                "mock_mode": mode,
                "output_json": "{}",
                "skill": skill_name,
                "confidence": "none",
                "latency_ms": "0",
            })
        finally:
            conn.close()

    except SkillError as exc:
        return error_result(str(exc))
    except Exception as exc:
        return error_result(str(exc))
