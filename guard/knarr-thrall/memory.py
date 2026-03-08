"""Thrall Switchboard — Structured memory + Living Memory Pillars.

Two layers:
1. **Structured memory** (thrall_memory table) — classified decision records
   for targeted recall. Query by skill, node_id, outcome.
2. **Living memory pillars** (rag/90-93-memory-*.md) — agent-maintained RAG
   documents that accumulate operational wisdom across sessions. Survives
   context resets, provides strategic continuity.

Pillar domains:
- operations (90): what worked/failed, operational patterns
- peers (91): CRM — peer behavior, reliability, preferences
- strategy (92): strategic decisions, rationale, outcomes
- felag (93): group agreements, policies, shared context
"""

import logging
import os
import time
from typing import Dict, List, Optional

from db import ThrallDB

logger = logging.getLogger("thrall.memory")

# ── Living Memory Pillar domains ─────────────────────────────────────

MEMORY_DOMAINS = {
    "operations": "90-memory-operations.md",
    "peers": "91-memory-peers.md",
    "strategy": "92-memory-strategy.md",
    "felag": "93-memory-felag.md",
}


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


# ── Living Memory Pillar Writer ──────────────────────────────────────

class MemoryWriter:
    """Append-only writer for living memory RAG pillars.

    Each domain maps to a markdown file in rag/. Entries are timestamped
    and appended. The file is created with a header if missing.
    """

    def __init__(self, rag_dir: str):
        self._rag_dir = rag_dir
        os.makedirs(rag_dir, exist_ok=True)

    def _pillar_path(self, domain: str) -> str:
        filename = MEMORY_DOMAINS.get(domain)
        if not filename:
            raise ValueError(f"Unknown memory domain: {domain}. "
                             f"Valid: {list(MEMORY_DOMAINS.keys())}")
        return os.path.join(self._rag_dir, filename)

    def append(self, domain: str, entry: str, timestamp: float = None):
        """Append a timestamped entry to a memory pillar.

        Creates the file with a header if it doesn't exist.
        """
        path = self._pillar_path(domain)
        ts = timestamp or time.time()
        ts_str = time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts))

        # Create file with header if missing
        if not os.path.exists(path):
            header = self._make_header(domain)
            with open(path, "w", encoding="utf-8") as f:
                f.write(header)

        # Append entry with timestamp
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n## {ts_str}\n{entry}\n")

        logger.info(f"MEMORY_PILLAR {domain}: appended entry ({len(entry)} chars)")

    def read(self, domain: str) -> str:
        """Read full pillar content. Returns empty string if file missing."""
        path = self._pillar_path(domain)
        if not os.path.exists(path):
            return ""
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def line_count(self, domain: str) -> int:
        """Count lines in pillar. Returns 0 if file missing."""
        path = self._pillar_path(domain)
        if not os.path.exists(path):
            return 0
        with open(path, "r", encoding="utf-8") as f:
            return sum(1 for _ in f)

    def _make_header(self, domain: str) -> str:
        """Generate initial header for a new pillar file."""
        titles = {
            "operations": "Operational Memory",
            "peers": "Peer Knowledge",
            "strategy": "Strategic Memory",
            "felag": "Félag Memory",
        }
        title = titles.get(domain, domain.title())
        return f"# {title}\n\n"
