"""Leaf skill with Ollama dependency.

Runs inference against a local Ollama model. Implements healthcheck()
to verify Ollama is reachable — this lets chain orchestrators know
whether this skill is available before committing to a job.

The healthcheck also triggers model loading as a side effect,
so the model is warm by the time the real request arrives.

knarr.toml:
    [skills.summarize-lite]
    handler = "skills/summarize_lite.py:handle"
    description = "Summarize text using a local LLM"
    input_schema = {text = "string"}
    price = 2.0
    visibility = "public"
    slow = true
"""

import json
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(__file__))
from skill_base import SkillBase

OLLAMA_BASE = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
MODEL = "qwen3:14b"


class SummarizeSkill(SkillBase):
    name = "summarize-lite"
    required_fields = ["text"]

    async def healthcheck(self):
        """Verify Ollama is reachable and responding."""
        resp = requests.get(f"{OLLAMA_BASE}/api/tags", timeout=5)
        resp.raise_for_status()

    async def run(self, data):
        text = data["text"]

        resp = requests.post(
            f"{OLLAMA_BASE}/api/generate",
            json={
                "model": MODEL,
                "prompt": f"Summarize the following text in 2-3 sentences:\n\n{text}",
                "stream": False,
                "options": {"num_predict": 200, "temperature": 0.3},
            },
            timeout=120,
        )
        resp.raise_for_status()
        result = resp.json()

        return {
            "summary": result.get("response", ""),
            "model": MODEL,
            "eval_count": str(result.get("eval_count", 0)),
        }


_skill = SummarizeSkill()


def set_node(node):
    _skill.set_node(node)


async def handle(input_data: dict) -> dict:
    return await _skill.handle(input_data)
