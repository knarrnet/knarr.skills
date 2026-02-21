"""Minimal leaf skill example.

Accepts a text input, returns it reversed. No external dependencies.
This is the simplest possible skill using SkillBase.

knarr.toml:
    [skills.reverse-text-lite]
    handler = "skills/reverse_text_lite.py:handle"
    description = "Reverse a text string"
    input_schema = {text = "string"}
    price = 0.1
    visibility = "public"
"""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from skill_base import SkillBase


class ReverseTextSkill(SkillBase):
    name = "reverse-text-lite"
    required_fields = ["text"]

    async def run(self, data):
        text = data["text"]
        return {
            "reversed": text[::-1],
            "length": str(len(text)),
        }


_skill = ReverseTextSkill()


def set_node(node):
    _skill.set_node(node)


async def handle(input_data: dict) -> dict:
    return await _skill.handle(input_data)
