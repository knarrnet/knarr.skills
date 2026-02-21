"""Chain skill (orchestrator) example.

Calls two sub-skills and merges their results. Declares its chain
dependencies so the base class can healthcheck the full pipeline.

The pre-exec check_chain() call is optional but recommended for
expensive chains — it catches broken links in <5s instead of failing
after minutes of wasted GPU time.

knarr.toml:
    [skills.research-audit-lite]
    handler = "skills/research_audit_lite.py:handle"
    description = "Research a topic and audit the sources"
    input_schema = {topic = "string"}
    price = 5.0
    visibility = "public"
    slow = true
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from skill_base import SkillBase


class ResearchAuditSkill(SkillBase):
    name = "research-audit-lite"
    chain = ["web-fetch-clean", "summarize-lite"]
    required_fields = ["topic"]
    call_local_timeout = 300_000  # 5 min

    async def run(self, data):
        topic = data["topic"]

        # Optional: pre-exec healthcheck. Catches dead links before
        # committing to expensive work. Remove if you prefer to rely
        # on the periodic health monitor (Phase 2).
        chain_err = await self.check_chain()
        if chain_err:
            return {"error": f"Chain unhealthy, aborting: {chain_err}"}

        # Step 1: Fetch content
        fetch_result = await self.call("web-fetch-clean", {
            "url": f"https://en.wikipedia.org/wiki/{topic}",
        })
        if fetch_result.get("error"):
            return {"error": f"Fetch failed: {fetch_result['error']}"}

        content = fetch_result.get("text", "")

        # Step 2: Summarize
        summary_result = await self.call("summarize-lite", {
            "text": content[:10000],  # truncate for model context
        })
        if summary_result.get("error"):
            return {"error": f"Summarize failed: {summary_result['error']}"}

        return {
            "topic": topic,
            "summary": summary_result.get("summary", ""),
            "source_url": fetch_result.get("url", ""),
            "model": summary_result.get("model", ""),
            "status": "ok",
        }


_skill = ResearchAuditSkill()


def set_node(node):
    _skill.set_node(node)


async def handle(input_data: dict) -> dict:
    return await _skill.handle(input_data)
