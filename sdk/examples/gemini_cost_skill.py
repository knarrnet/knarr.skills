"""Example: Leaf skill with Gemini cost tracking (v0.29.0+).

Demonstrates:
- call_gemini_with_usage() for token/cost tracking
- add_ext_cost() to report external API costs
- self_cost for declared compute overhead
- Cost fields automatically injected into response by SkillBase
"""

from skill_base import SkillBase
from gemini_client import call_gemini_with_usage


class SummarySkill(SkillBase):
    name = "summary-lite"
    required_fields = ["text"]
    self_cost = 0.05  # declared compute overhead (USD)

    async def run(self, data):
        text, usage = call_gemini_with_usage(
            data["gemini_api_key"],
            "Summarize the following text in 2-3 sentences.",
            data["text"],
        )
        # Record external cost — feeds into _cost_ext in response
        self.add_ext_cost(usage["ext_cost_usd"], "gemini-flash")

        return {
            "summary": text,
            "tokens_used": str(usage["total_tokens"]),
            "status": "ok",
        }


# Module exports
_skill = SummarySkill()


def set_node(node):
    _skill.set_node(node)


async def handle(input_data: dict) -> dict:
    return await _skill.handle(input_data)
