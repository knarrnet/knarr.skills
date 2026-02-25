"""Scheduled jobs: timer tracking, bulk data pull, context assembly."""

import json
import sqlite3
import time
from typing import Any, Dict, List, Optional

from memory import AgentMemory


class ScheduledJob:
    def __init__(self, name: str, config: Dict[str, Any]):
        self.name = name
        self.enabled = config.get("enabled", False)
        self.interval_seconds = float(config.get("interval_hours", 6)) * 3600
        self.prompt_template = config.get("prompt", "")
        self.window_hours = int(config.get("window_hours", 24))
        self.run_at_utc = config.get("run_at_utc")
        self._extra_config = config


class Scheduler:
    def __init__(self, config: Dict[str, Any], memory: AgentMemory):
        self._memory = memory
        self._jobs: Dict[str, ScheduledJob] = {}
        jobs_cfg = config.get("jobs", {})
        for name, job_cfg in jobs_cfg.items():
            if isinstance(job_cfg, dict):
                self._jobs[name] = ScheduledJob(name, job_cfg)

    def get_due_jobs(self) -> List[ScheduledJob]:
        """Return jobs that are due to run."""
        now = time.time()
        due = []
        for name, job in self._jobs.items():
            if not job.enabled:
                continue
            last_run_str = self._memory.get_state(f"job_last_run:{name}", "0")
            last_run = float(last_run_str)
            if (now - last_run) >= job.interval_seconds:
                due.append(job)
        return due

    def mark_ran(self, job: ScheduledJob):
        self._memory.set_state(f"job_last_run:{job.name}", str(time.time()))

    def pull_task_stats(self, storage_path: str, window_hours: int = 24) -> Dict[str, Any]:
        """Pull execution statistics from node.db (read-only)."""
        try:
            conn = sqlite3.connect(f"file:{storage_path}?mode=ro", uri=True)
            conn.execute("PRAGMA query_only = ON")
        except Exception:
            return {"error": "Cannot open node.db", "stats_summary": "unavailable"}

        try:
            since = time.time() - (window_hours * 3600)

            # Total executions
            total = conn.execute(
                "SELECT COUNT(*) FROM execution_log WHERE created_at > ?", (since,)
            ).fetchone()[0]

            # By status
            status_rows = conn.execute(
                "SELECT status, COUNT(*) FROM execution_log WHERE created_at > ? GROUP BY status",
                (since,)
            ).fetchall()
            by_status = {r[0]: r[1] for r in status_rows}

            # By skill (top 10)
            skill_rows = conn.execute(
                "SELECT skill_name, COUNT(*), AVG(wall_time_ms), SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) "
                "FROM execution_log WHERE created_at > ? GROUP BY skill_name ORDER BY COUNT(*) DESC LIMIT 10",
                (since,)
            ).fetchall()
            by_skill = [
                {"skill": r[0], "count": r[1], "avg_ms": round(r[2] or 0), "failures": r[3]}
                for r in skill_rows
            ]

            # Recent failures
            failures = conn.execute(
                "SELECT skill_name, error, created_at FROM execution_log "
                "WHERE created_at > ? AND status = 'failed' ORDER BY created_at DESC LIMIT 5",
                (since,)
            ).fetchall()
            recent_failures = [
                {"skill": r[0], "error": (r[1] or "")[:200], "at": r[2]}
                for r in failures
            ]

            conn.close()

            summary_lines = [
                f"Total executions: {total}",
                f"By status: {json.dumps(by_status)}",
                f"Top skills: {json.dumps(by_skill)}",
            ]
            if recent_failures:
                summary_lines.append(f"Recent failures: {json.dumps(recent_failures)}")

            return {
                "total": total,
                "by_status": by_status,
                "by_skill": by_skill,
                "recent_failures": recent_failures,
                "stats_summary": "\n".join(summary_lines),
                "window_hours": window_hours,
            }
        except Exception as e:
            conn.close()
            return {"error": str(e), "stats_summary": f"Error pulling stats: {e}"}

    def pull_daily_digest(self, storage_path: str, memory: AgentMemory) -> Dict[str, Any]:
        """Pull data for a daily digest."""
        now = time.time()
        since_24h = now - 86400

        event_count = memory.count_events_since(since_24h)
        action_count = memory.count_events_since(since_24h, "action_taken")

        # Pull execution summary from node.db
        stats = self.pull_task_stats(storage_path, window_hours=24)

        return {
            "event_count": event_count,
            "action_count": action_count,
            "execution_summary": stats.get("stats_summary", "unavailable"),
            "peer_summary": "N/A",
            "operator_node": "",
        }
