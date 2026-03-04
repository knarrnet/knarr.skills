"""Thrall Switchboard — Structured memory.

Classified decision memory, grouped by input identifiers (skill, node_id,
mail_id, outcome). Not blobs — every record is tagged for targeted recall.

Query patterns:
- "Last 5 settlements with peer X"
- "Rejection rate for peer Y"
- "All outcomes for skill Z in last 24h"

Two consumers: thrall recipes (via gather stage) and agent plugin (via DB).
Dryrun-sensitive: records marked dryrun=1 are excluded from operational
queries by default. Swarm experiments get clean isolation.
"""

import logging
import time
from typing import Dict, List, Optional

from db import ThrallDB

logger = logging.getLogger("thrall.memory")


class ThrallMemory:
    """Structured decision memory for thrall recipes."""

    def __init__(self, db: ThrallDB):
        self._db = db

    def record(self, skill: str, node_id: str, outcome: str,
               mail_id: str = "", amount: float = 0.0,
               reasoning: str = "", metadata: dict = None,
               dryrun: bool = False) -> int:
        """Record a decision outcome.

        Returns the row ID of the inserted record.
        """
        row_id = self._db.record_memory(
            skill=skill, node_id=node_id, outcome=outcome,
            mail_id=mail_id, amount=amount, reasoning=reasoning,
            metadata=metadata, dryrun=dryrun)
        logger.debug(f"MEMORY_RECORD skill={skill} peer={node_id[:16]} "
                     f"outcome={outcome} amount={amount:.1f} "
                     f"{'[dryrun]' if dryrun else ''}")
        return row_id

    def query(self, node_id: str = None, skill: str = None,
              outcome: str = None, limit: int = 10,
              since: float = None, include_dryrun: bool = False) -> List[dict]:
        """Query memory with flexible filters."""
        return self._db.query_memory(
            node_id=node_id, skill=skill, outcome=outcome,
            limit=limit, since=since, include_dryrun=include_dryrun)

    def get_peer_summary(self, node_id: str, skill: str = None,
                         days: int = 7) -> dict:
        """Aggregated peer interaction summary."""
        since = time.time() - (days * 86400)
        records = self.query(node_id=node_id, skill=skill,
                             since=since, limit=100)

        if not records:
            return {"total": 0, "outcomes": {}}

        outcomes: Dict[str, int] = {}
        total_amount = 0.0
        for r in records:
            o = r["outcome"]
            outcomes[o] = outcomes.get(o, 0) + 1
            total_amount += r.get("amount", 0)

        return {
            "total": len(records),
            "outcomes": outcomes,
            "total_amount": round(total_amount, 2),
            "last_interaction": records[0]["timestamp"],
            "skills": list(set(r["skill"] for r in records)),
        }

    def format_for_prompt(self, node_id: str = None, skill: str = None,
                          limit: int = 5) -> str:
        """Format memory as text for LLM prompt injection."""
        records = self.query(node_id=node_id, skill=skill, limit=limit)
        if not records:
            return "No prior interactions recorded."

        lines = [f"Recent interactions ({len(records)} records):"]
        for r in records:
            ts = time.strftime("%m-%d %H:%M", time.gmtime(r["timestamp"]))
            peer = r["node_id"][:12] if r["node_id"] else "?"
            lines.append(
                f"  {ts} | {r['skill']} | peer={peer} | "
                f"outcome={r['outcome']} | amount={r.get('amount', 0):.1f}"
            )
            if r.get("reasoning"):
                lines.append(f"    reason: {r['reasoning'][:80]}")
        return "\n".join(lines)
